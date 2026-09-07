# Extra Compose files layered onto the demo stack. Empty everywhere by design;
# the CI synthetic-load job is the one caller that sets it, to
# `-f docker-compose.ci-load.yml`, which puts Postgres's data directory on tmpfs
# so the runner's block device is not part of the latency measurement (ADR 0010,
# 2026-08-28). It is a variable rather than a second DEMO_COMPOSE because the
# override has to apply to *every* demo-* target in that job: a tmpfs data
# directory starts empty, so a stack seeded without it and measured with it
# would measure an empty database.
DEMO_COMPOSE_EXTRA ?=
DEMO_COMPOSE = docker compose -p movielens-demo -f docker-compose.yml -f docker-compose.demo.yml $(DEMO_COMPOSE_EXTRA)
# The same stack with the `load` profile enabled. `up` deliberately uses the
# plain invocation -- the load generator and its multi-worker API are started
# by the gate, not by a demo -- but anything that claims to cover the whole
# project has to name the profile, or Compose does not consider those services
# to exist: `demo-down` after a load run left `api-load` up and then failed to
# remove the network it was still attached to.
DEMO_COMPOSE_ALL = $(DEMO_COMPOSE) --profile load
K6_VERSION := $(strip $(shell cat infra/ci/k6-version))

# --- Production-mode rehearsal stack ----------------------------------------
# A second, deliberately separate Compose project: its own name, its own
# volumes, its own images (:prod, not :demo) and no published port except the
# TLS edge, so it can run alongside the demo stack without either one touching
# the other's state. Nothing here reads docker-compose.yml -- the dev stack's
# trust authentication and published data-store ports are exactly what this
# stack exists not to inherit.
PROD_ENV_FILE ?= .env.prod
PROD_COMPOSE = docker compose -p movielens-prod -f docker-compose.prod.yml --env-file $(PROD_ENV_FILE)
# The release, verification and backup jobs sit behind the `jobs` profile so
# `up` never starts them. `run` enables a service's own profile on its own;
# `build` and `down` have to be told.
PROD_COMPOSE_ALL = $(PROD_COMPOSE) --profile jobs

# --- Staging ----------------------------------------------------------------
# The same production file with docker-compose.staging.yml layered on top: a
# different Compose project and ENVIRONMENT=staging on the eight services that
# carry an environment label, and nothing else. The file order matters -- the
# overlay is second so its `name` and its environment overrides win -- and the
# project name is passed on the command line as well, so a hand-typed invocation
# in the wrong order still cannot address movielens-prod's volumes.
#
# Staging deliberately gets a fraction of production's targets. It rehearses a
# release: pull, release, serve, verify, and the disposal that makes the next
# rehearsal start from empty volumes. Deploys, rollbacks, backups, the rollback
# rehearsal and the advisory load run belong to the box, and adding them here
# would invite someone to run the deployment's operational path against the
# throwaway environment. The Compose services still exist behind the `jobs`
# profile for anyone who genuinely needs one by hand.
STAGING_ENV_FILE ?= .env.staging
STAGING_COMPOSE = docker compose -p movielens-staging \
	-f docker-compose.prod.yml -f docker-compose.staging.yml \
	--env-file $(STAGING_ENV_FILE)
STAGING_COMPOSE_ALL = $(STAGING_COMPOSE) --profile jobs

# Where the load gate leaves its evidence: the k6 summary, the raw sample
# stream, the per-second latency table, and container CPU snapshots from either
# side of the measured window. CI uploads it whether the gate passed or failed,
# because a passing run is the baseline the next failure gets compared against.
# Keep the leading `./` — Compose reads a bind-mount source without one as a
# named volume.
LOAD_RESULTS_DIR ?= ./artifacts/load-smoke
# The page-shaped workloads write next to the recommendation gate's evidence
# rather than into it: two workloads, two baselines, one uploaded artifact tree.
PAGE_RESULTS_DIR ?= ./artifacts/load-pages
# Whether a breached page latency budget fails the run. Correctness is always
# enforced. PR CI leaves this false while the budgets are new (see ADR 0010's
# page-shaped note for what promoting them requires); the nightly run sets it
# true. Never flip this to false to make a red build green.
PAGE_LATENCY_ENFORCED ?= false
# Uvicorn workers on the load-serving process, in one place because the k6
# warm-up sizes itself from the same value. A warm-up that primes fewer
# processes than exist leaves cold ones inside the measured window.
API_LOAD_WORKERS ?= 4
# The gate itself lives in synthetic/load/run_gate.sh: one k6 window, the
# evidence around it, and the documented re-measure rule. It is a script rather
# than a recipe because "run, decide, maybe run once more" reads badly as
# nested make.
LOAD_GATE = API_LOAD_WORKERS=$(API_LOAD_WORKERS) K6_VERSION=$(K6_VERSION) \
	DEMO_COMPOSE="$(DEMO_COMPOSE)" sh synthetic/load/run_gate.sh

# How long `prod-verify` pauses between its two stages. Both authenticate as
# the same `verify` account, so under ADR 0014 they charge one token bucket:
# `verify --all` spends ~30 of it and the reliability suite then spends ~40
# more within seconds. At the *first* defaults (120/minute, burst 30) the tail
# of the second stage came back 429 -- which surfaced as `cursor_rejection`
# reporting a catalog page that "offered no continuation cursor", because a
# throttled read has no `page` key. At the shipped defaults (600/minute, burst
# 120) those ~70 requests fit inside one burst and the collision no longer
# happens, so this pause is no longer what makes the target work.
#
# It is kept anyway, for 20 seconds of a several-minute target: the limit is a
# tuning knob and the failure it prevents is a confusing one to re-diagnose, so
# lowering the limit should cost a slower verify rather than a red run that
# blames the catalog. Raising the API's limit to make a chained target fit
# would still be the wrong repair.
PROD_VERIFY_COOLDOWN_SECONDS ?= 20

# --- Deterministic serving-artifact build -----------------------------------
# The committed bundle in infra/model-bundle/ is rebuilt and hash-compared by
# CI on linux/amd64, while most work on this project happens on arm64 macOS.
# LightGBM's text model is not byte-identical across architectures, so the
# build runs inside the features image pinned to linux/amd64 rather than
# against the host interpreter: the bundle is produced on the architecture
# that checks it. That is also why these targets do not simply invoke
# `python -m src.training.demo_artifacts` the way the other train-* targets do.
#
# ARTIFACT_AS_OF is a literal and is never computed. It becomes the manifest's
# trained_at, which is what makes manifest.json byte-stable across rebuilds,
# and it sits after the frozen persona fixture's last event so every seeded
# rating is in scope (synthetic/personas/seed.py pins both).
ARTIFACT_AS_OF := 2026-09-01T00:00:00+00:00
ARTIFACT_PLATFORM ?= linux/amd64
ARTIFACT_IMAGE ?= movielens-recsys/features:artifacts
ARTIFACT_DIR ?= infra/model-bundle

# The build reads the ratings table directly as admin_user. The defaults reach
# the demo Compose stack's Postgres; CI overrides them to reach its own.
ARTIFACT_NETWORK ?= movielens-demo_default
ARTIFACT_DB_HOST ?= postgres
ARTIFACT_DB_PORT ?= 5432
ARTIFACT_DB_NAME ?= movielens
ARTIFACT_DB_USER ?= admin_user
ARTIFACT_DB_PASSWORD ?= admin_user
ARTIFACT_TENANT ?= demo
# The thread pins repeat what the features image already bakes: this is the
# one invocation whose output is compared byte for byte, so it states its own
# conditions rather than inheriting them. PYTHONHASHSEED keeps any
# string-keyed iteration stable for the same reason. The model-server token is
# set only because the image declares ENVIRONMENT=production and Settings
# refuses its dev default there; this container never speaks to the sidecar.
# The pgBouncer admin password is here for exactly the same reason and with
# exactly as little meaning: this build talks to Postgres directly as
# admin_user and never opens the pooler's admin console. Both are non-default
# literals rather than ENVIRONMENT=dev, because disarming every production
# guard inside the one build whose point is reproducibility is the worse trade.
ARTIFACT_RUN = docker run --rm --platform $(ARTIFACT_PLATFORM) \
	--network $(ARTIFACT_NETWORK) \
	-e ADMIN_USER_DB_HOST=$(ARTIFACT_DB_HOST) \
	-e ADMIN_USER_DB_PORT=$(ARTIFACT_DB_PORT) \
	-e ADMIN_USER_DB_NAME=$(ARTIFACT_DB_NAME) \
	-e ADMIN_USER_DB_USER=$(ARTIFACT_DB_USER) \
	-e ADMIN_USER_DB_PASSWORD=$(ARTIFACT_DB_PASSWORD) \
	-e MODEL_TENANT_ID=$(ARTIFACT_TENANT) \
	-e MODEL_SERVER_AUTH_TOKEN=serving-artifact-build \
	-e PGBOUNCER_ADMIN_PASSWORD=serving-artifact-build \
	-e PYTHONHASHSEED=0 \
	-e OMP_NUM_THREADS=1 -e OPENBLAS_NUM_THREADS=1 \
	-e MKL_NUM_THREADS=1 -e VECLIB_MAXIMUM_THREADS=1

# --- ADR 0011 cold-start cohort ----------------------------------------------
# DVC-tracked, regenerable, and picked up automatically by every train-* target
# that finds it at this path.
SYNTH_COLD_PARQUET ?= data/synthetic/cold_start/v1/users.parquet

.PHONY: install lint format typecheck test train train-popularity train-cf train-itemitem train-last-item train-content train-twotower train-sasrec sasrec-popularity-order train-ranker train-sasrec-ranker train-sasrec-ranker-bundles train-sasrec-ranker-scores gate gate-retrieval retrieval-tolerance-study serving-artifacts serving-artifacts-image serving-artifacts-check serving-artifacts-publish serving-artifacts-verify serve infra-up infra-down data-download data-ingest data-ingest-reset eda tmdb-ingest tmdb-load tmdb-coverage synth-cold-cohort db-migrate db-migrate-down db-migrate-status promote promote-revert catalog-verify up-dev demo-up demo-down demo-reset demo-seed demo-materialize demo-smoke demo-audits demo-load-quiesce demo-load-smoke demo-load-nightly demo-load-pages demo-load-pages-nightly demo-reliability-check demo-logs prod-env-guard up-prod prod-stores prod-pull prod-keycloak-provision prod-release prod-serve prod-seed prod-deploy prod-rollback prod-verify prod-load prod-rollback-rehearsal prod-backup prod-edge-ca prod-logs prod-down prod-reset staging-env-guard up-staging staging-stores staging-pull staging-release staging-serve staging-verify staging-edge-ca staging-logs staging-down staging-reset keycloak-export-realms web-install web-dev web-lint web-typecheck web-test web-e2e web-build diagrams api-contract api-contract-check web-api-types web-api-types-check

install:
	pip install -e ".[dev]"

# notebooks/ is in this list because CI has always linted it. Leaving it out
# locally meant `make lint` could pass on a tree the lint job then failed.
lint:
	ruff check src/ synthetic/ tests/ notebooks/
	black --check src/ synthetic/ tests/ notebooks/

format:
	ruff check --fix src/ synthetic/ tests/ notebooks/
	black src/ synthetic/ tests/ notebooks/

typecheck:
	mypy src/ synthetic/ notebooks/

api-contract:
	python -m scripts.generate_openapi

api-contract-check:
	python -m scripts.generate_openapi --check

web-api-types:
	cd web && npm run api:types

web-api-types-check:
	cd web && npm run api:types:check

test:
	pytest tests/ -v

test-unit:
	pytest tests/unit/ -v

test-integration:
	pytest tests/integration/ -v

test-feature-parity:
	pytest tests/feature_parity/ -v

# The four trainers below take one optional environment variable,
# SYNTH_COLD_ROUTING, which decides where a learned model's fallback boundary
# sits. Unset (the default) is the index-membership rule every offline model
# has always used; `threshold` applies ADR 0001's COLD_START_THRESHOLD, the
# rule the deployed serving path uses. It exists so the two can be compared —
# see docs/cold-start-routing-decision.md — and it changes nothing unless set.
# The popularity baseline ignores it: it *is* the fallback and has no learned
# path to route away from.
#
#   make train-itemitem                            # index membership (default)
#   SYNTH_COLD_ROUTING=threshold make train-itemitem
train-popularity:
	python -m src.training.popularity

train-cf:
	python -m src.training.cf

train-itemitem:
	python -m src.training.itemitem

# The control a sequential retriever has to beat (decision D-003): score each
# candidate by how often it followed the user's most recent item. Same split,
# K, exclusions, routing and evaluator as every other candidate model.
#   LASTITEM_USER_SAMPLE_FRACTION=0.06 make train-last-item   # match a pilot
train-last-item:
	python -m src.training.last_item

# Content-based retrieval for cold items (ADR 0017, increment 1): score the
# whole `movies` catalog by genre and release-year similarity to the user's
# taste, so an item nobody has rated is reachable at all. Coverage is the claim
# this run supports; its recall numbers are a first measurement, not a verdict.
train-content:
	python -m src.training.content

train-twotower:
	python -m src.training.twotower

train-sasrec:
	python -m src.training.sasrec

# The popularity fill order a served SASRec bundle tops a short slate up from.
# `train-sasrec` writes it into every new artifact directory; this target is for
# the encoders exported before the role existed, whose directories are immutable.
# Pass the run's expected row count and cutoff — an ordering built from a
# different frame than the run fitted on is wrong in a way nothing downstream
# would notice:
#
#   make sasrec-popularity-order OUT=artifacts/sasrec/<run-id> \
#       EXPECT_ROWS=25000095 EXPECT_CUTOFF=1466837397
sasrec-popularity-order:
	python -m src.training.export_popularity_order --output-dir "$(OUT)" \
		$(if $(EXPECT_ROWS),--expect-rows $(EXPECT_ROWS),) \
		$(if $(EXPECT_CUTOFF),--expect-cutoff $(EXPECT_CUTOFF),)

train-ranker:
	python -m src.training.ranker

# Both arms of step 1 in one process: an item-item + LightGBM incumbent and a
# SASRec + LightGBM challenger, trained from the identical 30-day positives with
# #126's exclusions, then put through ADR 0001's gate. Reads the pinned SASRec
# artifact from SASREC_RANKER_ARTIFACT_DIR and writes each booster, before it is
# scored, under SASREC_RANKER_BOOSTER_DIR.
train-sasrec-ranker:
	python -m src.training.sasrec_ranker

# The two follow-on arms, once `train-sasrec-ranker` has left its boosters on
# disk: a per-route composition of them (no new weights) and one booster trained
# on the union of both arms' training sets. Both gated against the same item-item
# incumbent, neither promoted.
train-sasrec-ranker-bundles:
	python -m src.training.sasrec_ranker_bundles

# ADR 0018 increment 1: one new learned-route booster reading the eight
# aggregates plus the two point-in-time SASRec score columns, composed with the
# untouched fallback booster and gated against PR #151's per-route bundle. Reads
# the same pinned artifact and booster directory as the two targets above.
train-sasrec-ranker-scores:
	python -m src.training.sasrec_ranker_scores

# The seed the two stochastic trainers use. CF/ALS initialises its factors at
# random and the ranker samples its positives, its negatives and its splits;
# popularity and item-item have no random component and ignore this entirely.
# Unset reproduces every number in docs/results.md.
#
#   TRAIN_SEED=7 make train-cf
#
# Three seeds per model is what the promotion gate's slice tolerance was
# measured from — see src/evaluation/gate.py and docs/results.md.

# How many positives the ranker trains on. Unset is the whole 30-day trailing
# window, which is what makes a re-seeded ranker run comparable to the last one
# — see src/training/sampling.py and docs/results.md's sample-size section.
# Popularity, item-item, CF/ALS and the two-tower ignore it.
#
#   RANKER_POSITIVE_LIMIT=20000 make train-ranker

# ADR 0001's promotion gate over MLflow runs. Prints the verdict and exits 0 to
# promote, 1 to refuse, 2 when it cannot decide (a K mismatch, or a killed run
# with parameters and no metrics). Either side takes several space-separated
# run ids, in which case the gate reads their mean — the comparison to make for
# a model whose metrics move with the seed.
#
#   make gate CANDIDATE=<run id> INCUMBENT=<run id>
#   make gate CANDIDATE="<id> <id> <id>" INCUMBENT="<id> <id> <id>"
#   make gate CANDIDATE=<run id> INCUMBENT=<run id> GATE_ARGS="--scope learned-route"
# The default is all-routes. Use learned-route only when the change is confined
# to threshold-routed users; it gates warm +3% and cold non-regression while
# reporting overall without gating on it (ADR 0001, 2026-09-05 amendment).
gate:
	@test -n "$(CANDIDATE)" || { echo "usage: make gate CANDIDATE=<run id> INCUMBENT=<run id>"; exit 2; }
	@test -n "$(INCUMBENT)" || { echo "usage: make gate CANDIDATE=<run id> INCUMBENT=<run id>"; exit 2; }
	python -m src.evaluation.gate --candidate $(CANDIDATE) --incumbent $(INCUMBENT) $(GATE_ARGS)

# ADR 0004's retrieval-only gate. Unlike `gate`, this reads recall@500 and
# requires canonical protocol metadata plus the stated seed set. Retrieval
# tolerances intentionally have no defaults: they must come from the measured
# retrieval noise study, never from the ranker's NDCG tolerances.
#
#   make gate-retrieval \
#     CANDIDATE="<seed-42> <seed-7> <seed-13>" INCUMBENT=<item-item> \
#     RETRIEVAL_COLD_TOLERANCE=<measured fraction> \
#     RETRIEVAL_OVERALL_TOLERANCE=<measured fraction>
#
# RETRIEVAL_SEEDS states the seed policy and defaults to the three-seed set.
# Under the one-run-per-configuration policy, pass the single seed the run used:
#
#   make gate-retrieval CANDIDATE=<seed-42 run> INCUMBENT=<item-item> \
#     RETRIEVAL_SEEDS=42 RETRIEVAL_COLD_TOLERANCE=... RETRIEVAL_OVERALL_TOLERANCE=...
#
# The verdict then records seed_regime=single_seed, and the tolerances it is
# handed must have come from a study with no seed term.
gate-retrieval:
	@test -n "$(CANDIDATE)" || { echo "CANDIDATE run id(s) required"; exit 2; }
	@test -n "$(INCUMBENT)" || { echo "INCUMBENT run id(s) required"; exit 2; }
	@test -n "$(RETRIEVAL_COLD_TOLERANCE)" || { echo "measured RETRIEVAL_COLD_TOLERANCE required"; exit 2; }
	@test -n "$(RETRIEVAL_OVERALL_TOLERANCE)" || { echo "measured RETRIEVAL_OVERALL_TOLERANCE required"; exit 2; }
	python -m src.evaluation.retrieval_gate \
		--candidate $(CANDIDATE) --incumbent $(INCUMBENT) \
		--cold-tolerance $(RETRIEVAL_COLD_TOLERANCE) \
		--overall-tolerance $(RETRIEVAL_OVERALL_TOLERANCE) \
		$(if $(RETRIEVAL_SEEDS),--seeds $(RETRIEVAL_SEEDS),) \
		$(RETRIEVAL_GATE_ARGS)

# Where the two tolerances `gate-retrieval` demands are supposed to come from.
# Reads one evidence document of noise-study runs the gate does not read, and
# either proposes both fractions or refuses and says why -- 0 proposed, 1
# measured and declined, 2 could not measure. The rule and the evidence schema
# are docs/model-planning/contracts/retrieval-tolerance-measurement.md.
#
#   make retrieval-tolerance-study EVIDENCE=artifacts/sasrec-noise-study.json
retrieval-tolerance-study:
	@test -n "$(EVIDENCE)" || { echo "EVIDENCE=<path to study json> required"; exit 2; }
	python -m src.evaluation.tolerance_study --evidence $(EVIDENCE) $(TOLERANCE_STUDY_ARGS)

# Non-negotiable #5's entry point: a fixed seed and a fixed as-of produce the
# same artifact hashes. It is the serving-bundle build under the name the
# non-negotiable uses.
train: serving-artifacts

serving-artifacts-image:
	docker build --platform $(ARTIFACT_PLATFORM) \
		-f infra/features/Dockerfile -t $(ARTIFACT_IMAGE) .

# Training logs go to stderr; stdout carries the bundle out of the container as
# a tar stream, which sidesteps the uid mismatch a writable bind mount would
# hit between the image's `feastuser` and whatever user CI runs as. It lands in
# a file rather than a pipe so that a failed build fails this target instead of
# handing an empty archive to a tar that shrugs.
serving-artifacts: serving-artifacts-image
	@mkdir -p $(ARTIFACT_DIR) artifacts
	$(ARTIFACT_RUN) $(ARTIFACT_IMAGE) sh -c \
		'python -m src.training.demo_artifacts --train-only \
			--as-of $(ARTIFACT_AS_OF) --output-dir /tmp/bundle >&2 \
			&& tar -c -C /tmp/bundle .' > artifacts/serving-bundle.tar
	tar -x -C $(ARTIFACT_DIR) -f artifacts/serving-bundle.tar

# THE DEMO FIXTURE'S CHECK, and only that one. Rebuilds into a scratch
# directory inside the container and fails if the committed bundle differs. The
# mount is read-only: a check must never be able to repair what it is checking.
#
# "Check a serving bundle" now means two different things, and they are two
# targets because they are two different claims. This one is a retrain-and-diff
# and it is non-negotiable #5's reproducibility gate — it only works because the
# demo fixture is trained from the demo tenant's ratings, which are right there.
# `serving-artifacts-verify` below is the other meaning. Neither replaces the
# other and this one does not weaken.
serving-artifacts-check: serving-artifacts-image
	$(ARTIFACT_RUN) -v "$(CURDIR)/$(ARTIFACT_DIR):/app/committed:ro" $(ARTIFACT_IMAGE) \
		python -m src.training.demo_artifacts --check \
			--as-of $(ARTIFACT_AS_OF) --output-dir /app/committed

# --- Schema 2 served bundle --------------------------------------------------
# A served bundle is assembled — not trained — from artifacts fit on the full
# dataset, which is why it needs a publisher of its own and why its check is not
# the one above. Rebuilding it from the demo tenant's ratings is impossible and
# would prove nothing, so `serving-artifacts-verify` checks what can be checked
# without the training data: every artifact re-hashed against the manifest,
# every manifest validator re-run (family roles and params, the per-route ranker
# feature contract against the booster files, the cross-route permutation
# guard), the kind marker matched, and lineage required.
#
# The two bundles live in separate directories rather than sharing one under a
# filename convention. They have the same file layout, so a single wrong
# MODEL_ARTIFACT_DIR is all it takes to serve the compact fixture in production,
# and a directory is the only distinction visible before anything is opened.
# `bundle-kind.json` is the mechanical half: it names the kind and pins the
# manifest's own checksum — the one file nothing else hashes — so a bundle moved
# into the other kind's directory fails the verify instead of loading quietly.
#
# Unlike `serving-artifacts` these run against the host interpreter. Nothing
# here trains: the publisher copies bytes somebody else produced, so LightGBM's
# cross-architecture text-model drift — the whole reason the fixture build is
# pinned to a linux/amd64 container — cannot apply to a copy.
SERVED_BUNDLE_DIR ?= infra/model-bundle-served
SERVED_BUNDLE_SPEC ?= infra/served-bundle.spec.json
# Pass --realise to also rebuild the models from the verified bundle, which for
# a sequence bundle rebuilds the encoder and its exact index. Off by default:
# it costs a torch import an item-item bundle has no use for.
SERVED_BUNDLE_ARGS ?=

# The spec names the artifacts to bake — which ones is an owner decision
# recorded in that document, never a default in here.
serving-artifacts-publish:
	@test -f $(SERVED_BUNDLE_SPEC) || { echo "no bundle spec at $(SERVED_BUNDLE_SPEC); write one (see src/models/bundle_publisher.py) or pass SERVED_BUNDLE_SPEC=<path>"; exit 2; }
	python -m src.models.bundle_publisher --kind served \
		--spec $(SERVED_BUNDLE_SPEC) --output-dir $(SERVED_BUNDLE_DIR) $(SERVED_BUNDLE_ARGS)

serving-artifacts-verify:
	python -m src.models.bundle_publisher --check --kind served \
		--output-dir $(SERVED_BUNDLE_DIR) $(SERVED_BUNDLE_ARGS)

serve:
	uvicorn src.serving.app:app --host 0.0.0.0 --port 8000 --reload

web-install:
	cd web && npm install

web-dev:
	cd web && npm run dev

web-lint:
	cd web && npm run lint

web-typecheck:
	cd web && npm run typecheck

web-test:
	cd web && npm test

web-e2e:
	cd web && npm run test:e2e

web-build:
	cd web && npm run build

# Re-renders docs/diagrams/src/*.mmd into the committed light/dark SVG pairs.
# Needs web/node_modules (make web-install) and the Playwright Chromium the
# browser suites already use. The output is deterministic, so a run that
# changes nothing leaves `git status` clean.
diagrams:
	cd web && npm run diagrams

infra-up:
	docker compose up -d

infra-down:
	docker compose down

data-download:
	python -m src.data.download

data-ingest:
	python -m src.data.ingest

data-ingest-reset:
	python -m src.data.ingest --reset

eda:
	python -m notebooks.eda

# --- TMDB catalog metadata (ADR 0017 increment 2) ---------------------------
# One snapshot of every TMDB detail payload the catalog can reach, versioned
# with DVC like the ratings frame so a model trained on it stays reproducible.
# Needs TMDB_READ_ACCESS_TOKEN in the environment and nowhere else:
#
#   set -a && . /path/to/tmdb.env && set +a
#   make tmdb-ingest ARGS="--limit 200"      # smoke first, ~10 seconds
#   make tmdb-ingest                         # ~52 min at the default 20 req/s
#   dvc add data/raw/tmdb/$(shell date +%F) && dvc push
#   make db-migrate && make tmdb-load        # into the normalised tables
#   make tmdb-coverage                       # the number ADR 0017 needs
#
# The pull is resumable and idempotent: it skips ids already in the shards, so
# re-running it after an interruption costs only what is missing. `tmdb-load`
# and `tmdb-coverage` need no token — they read the snapshot on disk.
tmdb-ingest:
	python -m src.data.tmdb_ingest $(ARGS)

tmdb-load:
	python -m src.data.tmdb_load $(ARGS)

tmdb-coverage:
	python -m src.data.tmdb_coverage --out docs/data/tmdb-coverage.md \
		--json-out docs/data/tmdb-coverage.json $(ARGS)

# ADR 0011's synthetic cold-start cohort. Reads ratings from Postgres, the way
# every train-* target does, and writes the DVC-tracked parquet the trainers
# pick up. Deterministic: a regeneration on the same seed and dataset version
# produces a byte-identical file, so `dvc status` stays clean unless something
# genuinely changed. On a machine with the CSVs on disk but nothing in
# Postgres, pass the file instead:
#   python -m synthetic.cold_start.generator --ratings-csv data/raw/ml-25m/ratings.csv
synth-cold-cohort:
	python -m synthetic.cold_start.generator --out $(SYNTH_COLD_PARQUET)

dvc-pull:
	dvc pull

dvc-push:
	dvc push

# --- Reviewed catalog fixture -----------------------------------------------
# HEAD every poster URL the fixture carries. Deliberately not a CI job: it
# asks a third party whether its CDN is up, and that must never be the reason
# a pull request cannot merge. The offline half — that every entry has a URL in
# the pinned image.tmdb.org/t/p/w500 shape — rides along in tests/unit and does
# gate CI. Run this by hand before committing a fixture change, and on the
# cadence a portfolio demo deserves (a poster path that 404s upstream is
# invisible until someone opens Browse).
# Needs no TMDB token; refilling the fixture does (see enrich_posters.py).
catalog-verify:
	python -m synthetic.personas.enrich_posters --verify

# --- Alembic migrations -----------------------------------------------------
# The Phase 3 tenant scaffolding (public.tenants, tenant_id columns, RLS
# policies, DB roles) is applied by Alembic. Run `db-migrate` after
# `data-ingest` on a fresh dev DB; run it standalone to catch up an
# existing DB that pre-dates the Phase 3 changes.
db-migrate:
	alembic upgrade head

db-migrate-down:
	alembic downgrade -1

db-migrate-status:
	alembic current

# --- Champion promotion ------------------------------------------------------
# Move one tenant's champion coordinates in public.tenants onto a serving
# bundle, after re-verifying every checksum that bundle pins. Migration 0016
# seeded the `demo` row and said that moving it "is a promotion"; this is that
# step. It is deliberately NOT Phase 6's automatic routing gate -- it decides
# nothing about whether a model is better, it registers the one an operator
# points it at.
#
# Runs on the host interpreter against the migrator DSN, the same way
# `db-migrate` does: a bundle directory and a migrator credential both have to
# be in reach, and the slim API image ships no LightGBM to read a manifest with.
# `--yes` is never passed from here, so a stray `make promote` in a shell that
# happens to hold production credentials refuses rather than repointing
# production; a deliberate production run says so on the command line.
#
# There is no TARGET or DATABASE variable here on purpose: the DSN comes from
# POSTGRES_* the same way every other offline entrypoint's does. Which is why
# the tool prints the host, port, database and tenant it resolved before it
# touches anything -- an ephemeral stack publishes Postgres on a port it had to
# choose, and `make promote TENANT=demo` in a shell that never set POSTGRES_PORT
# is talking to whatever is on 5432, usually this machine's own database.
#
#   POSTGRES_PORT=55432 make promote TENANT=demo BUNDLE=models/serving
PROMOTE_ARGS ?=

promote:
	@test -n "$(TENANT)" || { echo "usage: make promote TENANT=<tenant id> BUNDLE=<bundle dir>"; exit 2; }
	@test -n "$(BUNDLE)" || { echo "usage: make promote TENANT=<tenant id> BUNDLE=<bundle dir>"; exit 2; }
	python -m src.release.promote --tenant $(TENANT) --bundle $(BUNDLE) $(PROMOTE_ARGS)

# The undo, as its own target rather than PROMOTE_ARGS=--revert: whoever types
# this has a bad champion in front of them and should not also be assembling
# flags.
promote-revert:
	@test -n "$(TENANT)" || { echo "usage: make promote-revert TENANT=<tenant id>"; exit 2; }
	python -m src.release.promote --tenant $(TENANT) --revert $(PROMOTE_ARGS)

# --- Dev --------------------------------------------------------------------
# The dev environment's entry point, and deliberately an alias rather than a
# third Compose file.
#
# The multi-environment plan names docker-compose.{dev,staging,prod}.yml, but
# the dev stack already exists and already *is* two files: docker-compose.yml is
# the stores and a Keycloak with dev credentials, docker-compose.demo.yml is the
# application layer at ENVIRONMENT=dev over the demo fixture -- 515 interactions
# across nine users, which is the "smaller dataset snapshot in dev" the plan asks
# for. (The *catalog* is the full MovieLens 62,423 titles, because a retriever
# cannot be served ids the database has no rows for; it is the interactions that
# are small.) A
# docker-compose.dev.yml would have exactly one job left: turning DEV_AUTH_BYPASS
# on, which docker-compose.demo.yml explicitly sets to "false" so the browser
# journeys and the load gate run against real Keycloak tokens. Flipping it in an
# overlay on the same project would give one stack two auth behaviours depending
# on which target last ran, and Settings() already refuses the bypass outside
# dev on every service anyway.
#
# So: one dev stack, two files, and a name for it. `make up-dev` starts it;
# `make demo-seed` fills it. tests/unit/test_prod_compose.py holds the decision
# in place -- if a docker-compose.dev.yml ever appears, that test fails and asks
# whoever added it to revisit this comment rather than inherit it.
up-dev: demo-up

# --- Repeatable portfolio demo ----------------------------------------------
# The explicit project name isolates demo volumes from the normal dev stack.
# `demo-reset` is therefore destructive only to movielens-demo resources.
#
# The learned path has two halves, and starting the sidecars needs both: the
# serving bundle in the artifact volume, and rows in Redis for the tenant those
# artifacts were trained for. The model server's warm-up refuses to report
# healthy against an empty online store -- deliberately, because the
# alternative is ranking every candidate from missing features -- so starting
# it without the second half buys a 60-second `--wait` timeout and a stack
# trace where a one-line instruction belongs.
#
# DBSIZE is the cheap question (is there anything in the store at all?) rather
# than the precise one (are there rows for *this* tenant?). Nothing but Feast
# writes to this Redis, so an empty store is exactly the case a `demo-reset` or
# a removed volume leaves behind, and the sidecar's own guard still covers the
# partially-materialized case a key count cannot see. A probe that cannot
# answer at all falls through to starting the sidecars, which is what this
# target did before it had a probe.
demo-up:
	$(DEMO_COMPOSE) up -d --build --wait --wait-timeout 180 api web keycloak redis
	@if ! docker run --rm -v movielens-demo_model_artifacts:/artifacts \
		movielens-recsys/api:demo python -c \
		"from pathlib import Path; raise SystemExit(not Path('/artifacts/manifest.json').is_file())"; then \
		echo "No serving bundle yet, so the feature/model sidecars stay down and the API serves the popularity fallback. Run 'make demo-seed' to train and publish one."; \
	elif [ "$$($(DEMO_COMPOSE) exec -T redis redis-cli DBSIZE | tr -cd '0-9')" = "0" ]; then \
		echo "The serving bundle is present but the online feature store is empty, so the model sidecar would refuse to boot rather than rank from missing features. Run 'make demo-seed' to materialize it."; \
	else \
		$(DEMO_COMPOSE) up -d --wait --wait-timeout 60 feature-server model-server; \
	fi
	$(DEMO_COMPOSE) run --rm demo-setup python -m synthetic.smoke.demo --readiness-only --api-url http://api:8000 --web-url http://web:3001 --keycloak-url http://keycloak:8080

# `--build` because the fixture is baked into the image (`COPY synthetic`) and
# the seeder reads it from there: without it a refreshed catalog.json seeds an
# image-old snapshot, agrees with itself, and the demo keeps serving the posters
# and synopses it had yesterday. The layer is cached below the pip install, so
# the rebuild costs a few seconds and buys the guarantee that `make demo-seed`
# means what it says.
demo-seed:
	$(DEMO_COMPOSE) run --rm --build demo-setup python -c "from synthetic.personas.seed import main; main()"
	$(MAKE) demo-materialize

demo-materialize:
	$(DEMO_COMPOSE) build feature-setup
	$(DEMO_COMPOSE) run --rm feature-setup
	$(DEMO_COMPOSE) up -d --force-recreate --wait --wait-timeout 60 feature-server model-server

demo-smoke:
	$(DEMO_COMPOSE) run --rm demo-setup python -m synthetic.smoke.demo --api-url http://api:8000 --web-url http://web:3001 --keycloak-url http://keycloak:8080

demo-audits:
	$(DEMO_COMPOSE) run --rm demo-setup python -m synthetic.smoke.demo --audits-only --api-url http://api:8000 --web-url http://web:3001 --keycloak-url http://keycloak:8080

# Stop everything the load gate does not measure. The browser demo's `api`
# and `web` processes and the one-shot setup jobs are not on the measured
# path, but on a shared CI runner they compete for the same CPU as the
# processes that are — and CPU starvation there arrives as tail latency here.
# Containers are stopped, not removed, so `demo-logs` still explains a
# failure afterwards. Run between `demo-seed` and `demo-load-smoke`.
demo-load-quiesce:
	$(DEMO_COMPOSE) stop --timeout 20 web api demo-setup feature-setup

demo-load-smoke:
	@LOAD_PROFILE=smoke K6_PUSH_INTERVAL=2m LOAD_RESULTS_DIR=$(LOAD_RESULTS_DIR) $(LOAD_GATE)

demo-load-nightly:
	@LOAD_PROFILE=nightly K6_PUSH_INTERVAL=30s LOAD_RESULTS_DIR=./artifacts/load-nightly $(LOAD_GATE)

# The page-shaped budgets: what each route's fan-out, cursor continuation,
# library read, mutation-plus-read and quick-pick sequence cost, measured
# per step. Separate from the recommendation gate on purpose — that one is
# pinned and this one is still earning its thresholds.
demo-load-pages:
	@LOAD_PROFILE=smoke K6_PUSH_INTERVAL=2m LOAD_SCRIPT=/scripts/pages.js \
		LOAD_WORKLOAD=pages LOAD_LATENCY_ENFORCED=$(PAGE_LATENCY_ENFORCED) \
		LOAD_RESULTS_DIR=$(PAGE_RESULTS_DIR) $(LOAD_GATE)

demo-load-pages-nightly:
	@LOAD_PROFILE=nightly K6_PUSH_INTERVAL=30s LOAD_SCRIPT=/scripts/pages.js \
		LOAD_WORKLOAD=pages LOAD_LATENCY_ENFORCED=true \
		LOAD_RESULTS_DIR=./artifacts/load-pages-nightly $(LOAD_GATE)

# Non-latency serving promises that a percentile cannot express: the request id
# survives to the audit row, /healthz is reachable without a token while nothing
# else is, dependency provenance is visible somewhere real, degraded metadata
# renders instead of failing, and rate limiting answers 429 with a Retry-After
# where ADR 0014 turns it on.
# Runs against the same warm stack the load gate just measured.
demo-reliability-check:
	@mkdir -p $(PAGE_RESULTS_DIR)
	$(DEMO_COMPOSE) --profile load run --rm -T demo-setup \
		python -m synthetic.load.reliability \
		--api-url http://api-load:8000 --keycloak-url http://keycloak:8080 \
		> $(PAGE_RESULTS_DIR)/reliability.json

demo-down:
	$(DEMO_COMPOSE_ALL) down --remove-orphans

demo-reset:
	$(DEMO_COMPOSE_ALL) down --volumes --remove-orphans
	$(MAKE) demo-up
	$(MAKE) demo-seed

# `api-load` is in the list because the load gate's failure modes are read
# here: the runbook already sends you to its logs, and a service the profile
# hides is a service `logs` will not print. Naming a service with no container
# is quiet, so this is the same output as before on a stack that never ran the
# gate.
demo-logs:
	$(DEMO_COMPOSE_ALL) logs --tail=200 api api-load web demo-setup feature-server model-server postgres pgbouncer keycloak redis

# --- Production ------------------------------------------------------------
# These targets run the production stack, on the box and on a laptop. The order
# below is the release's real order, and it is not the demo stack's: Keycloak
# has to exist before its realms, the realms have to exist before the API can
# become ready (/readyz probes the serving realm's JWKS), and the schema has to
# exist before the sidecar's feature materialization can fence on it. Compose
# expresses only the first hop of that with depends_on, so the rest is these
# targets -- which is also why infra/deploy/deploy.sh drives them rather than
# spelling the sequence out a second time.
#
# The two halves of the same file:
#
#   on the box   deploy.sh exports IMAGE_TAG=<sha> and calls prod-pull,
#                prod-release, prod-serve and prod-verify. Nothing builds.
#   on a laptop  up-prod builds the images locally at the default tag and
#                prod-seed runs the same release steps against them.
#
# Nothing on this stack is destructive to `movielens-demo`: different project,
# different volumes, different image tags.

# Every secret in the stack is generated, so there is no default env file to
# fall back on. Failing here, with the two commands that fix it, beats failing
# four services later on an interpolation error.
prod-env-guard:
	@test -f $(PROD_ENV_FILE) || { \
		echo "$(PROD_ENV_FILE) is missing."; \
		echo "  cp infra/deploy/production.env.example $(PROD_ENV_FILE)"; \
		echo "  then replace every REPLACE_ME__ value with:"; \
		echo "    python -c \"import secrets; print(secrets.token_urlsafe(48))\""; \
		exit 1; }

# The laptop entry point: build every image from this checkout, then bring the
# stores up. The box never runs this -- it pulls what CI built and tested.
up-prod: prod-env-guard
	$(PROD_COMPOSE_ALL) build
	$(MAKE) prod-stores

# The data tier and identity, with the one-time role provisioning in between.
# Separate from up-prod because a deploy needs exactly this and no build: the
# release jobs cannot run until Postgres has the roles migration 0001 expects
# and pgBouncer can authenticate against them.
prod-stores: prod-env-guard
	$(PROD_COMPOSE) up -d --wait --wait-timeout 240 postgres-app postgres-keycloak redis edge
	$(PROD_COMPOSE) run --rm -T postgres-provision
	$(PROD_COMPOSE) up -d --wait --wait-timeout 300 pgbouncer keycloak

# Every image the compose model names, at whatever IMAGE_TAG is in the
# environment, followed by the assertion that matters: they are all here now.
# Without it a failed pull would be quietly repaired by `up` building the image
# from the checkout the box happens to have -- a release running something CI
# never tested, with nothing in the log to say so.
#
# DEPLOY_SKIP_PULL=1 skips the fetch and *only* the fetch: the presence check
# below still runs, so the rehearsal proves the same property the box does --
# every image the release needs is on this machine at this tag. It exists
# because the local rehearsal drives the real deploy.sh against locally built
# images that were tagged by hand, and GHCR has nothing to serve for a SHA that
# was never pushed. It is never set on the box; there the pull is the point.
prod-pull: prod-env-guard
	@if [ "$${DEPLOY_SKIP_PULL:-0}" = "1" ]; then \
		echo "DEPLOY_SKIP_PULL=1: skipping the registry fetch; the images must already be local"; \
	else \
		$(PROD_COMPOSE_ALL) pull; \
	fi
	@for image in $$($(PROD_COMPOSE_ALL) config --images | sort -u); do \
		docker image inspect "$$image" >/dev/null 2>&1 || { \
			echo "missing after pull: $$image"; exit 1; }; \
	done
	@echo "all images present at IMAGE_TAG=$${IMAGE_TAG:-main}"

# Realms, clients, the audience mapper and the three named accounts. Separate
# from prod-seed so R-6 can run it twice and confirm the second run reports no
# change -- a non-idempotent provisioning script is the kind of thing that only
# shows up on the second deploy.
prod-keycloak-provision: prod-env-guard
	$(PROD_COMPOSE) run --rm -T keycloak-provision

# The laptop's whole release: the state half and then the serving half. It is
# the same two steps a deploy runs, in the same order, which is what makes a
# rehearsal worth running. The serving tier is deliberately last -- the API
# cannot become ready until both the realm and the migrations exist, and a
# service that can never pass its healthcheck is a worse signal than one that
# has not been asked to start yet.
prod-seed: prod-env-guard
	$(MAKE) prod-release
	$(MAKE) prod-serve

# Everything a release does to state, and nothing that serves traffic: roles,
# realms, migrations, seed, feature materialization. Run before the new
# containers start, because the schema has to be ahead of the code that
# queries it. Idempotent end to end -- it runs on every deploy including a
# rollback, where the schema step correctly applies nothing.
prod-release: prod-env-guard
	$(MAKE) prod-stores
	$(MAKE) prod-keycloak-provision
	$(PROD_COMPOSE) run --rm -T release
	$(PROD_COMPOSE) run --rm -T materialize

# The serving tier. This is where a deploy's outage is: Compose recreates the
# containers whose image changed, and --wait holds until every one of them is
# healthy again -- which for the sidecar means warm, not merely listening.
prod-serve: prod-env-guard
	$(PROD_COMPOSE) up -d --wait --wait-timeout 300 feature-server model-server api web

# The post-deploy matrix, in the order the rows depend on each other: the
# in-deployment checks first (readiness, issuer equality, realm invariants,
# cold-start and learned serving, the write path, artifact provenance, the
# audit SLI), then cross-tenant isolation, then the non-latency serving
# promises. Each one exits non-zero on a finding, so `make` stops at the first.
#
# The reliability harness rides in the verify service rather than one of its
# own: it is in the same image, it wants the same identity, and the API
# image's entrypoint execs an unrecognised mode as given.
# The `canary` service is deliberately NOT run here. `verify --all` runs the
# same module as its V-6 row, so running it twice sweeps the isolation
# subject's 20 persona routes twice inside a second. At the first rate-limit
# defaults that was 40 requests against a 30-token burst, and the tail of the
# second sweep came back 429 -- with the canary reporting "expected 403 from
# the persona guard" on a request the limiter had answered before the guard
# ever saw it. The shipped burst of 120 absorbs 40, so today this is the
# harmless redundancy it always was rather than a failure; it stays out
# because a second identical sweep proves nothing either way. The standalone
# service stays in the compose file: it is the form an operator points at a
# target by hand, with every identity on the command line.
prod-verify: prod-env-guard
	$(PROD_COMPOSE) run --rm -T verify
	@echo "waiting $(PROD_VERIFY_COOLDOWN_SECONDS)s for the verify subject's rate-limit bucket to refill"
	@sleep $(PROD_VERIFY_COOLDOWN_SECONDS)
	$(PROD_COMPOSE) run --rm -T verify sh -c \
		'exec python -m synthetic.load.reliability \
			--api-url "$$API_URL" --keycloak-url "$$KEYCLOAK_URL" \
			--realm "$$VERIFY_REALM" --client-id "$$VERIFY_CLIENT_ID" \
			--client-secret "$$VERIFY_CLIENT_SECRET" \
			--username "$$VERIFY_USERNAME" --password "$$VERIFY_PASSWORD"'

# V-10. Deliberately weaker than the pinned CI gate, which needs Compose
# --force-recreate, docker stats and a cgroup probe and has no remote form:
# correctness and the warm-traffic learned assertion are enforced, p99 is
# recorded with no verdict. CI keeps the verdict.
prod-load: prod-env-guard
	$(PROD_COMPOSE) run --rm -T loadcheck

# R-12. Proves the pre-deploy schema step declines to act when the database is
# ahead of the image running it -- the difference between a rollback that ends
# an incident and one that starts a second.
prod-rollback-rehearsal: prod-env-guard
	$(PROD_COMPOSE) run --rm -T rollback-rehearsal

prod-backup: prod-env-guard
	$(PROD_COMPOSE) run --rm -T backup

# --- Deploys ----------------------------------------------------------------
# Both are one line into infra/deploy/deploy.sh, which owns the sequence, the
# release record in .release/ and the automatic rollback.
#
# `env -u IMAGE_TAG MAKEFLAGS=` is not decoration. A variable set on make's
# command line is exported to every sub-make as an override, and it beats an
# environment variable set inside the recipe -- so `make prod-deploy
# IMAGE_TAG=<sha>` would silently force IMAGE_TAG=<sha> on the sub-makes
# deploy.sh runs during a *rollback*, and the rollback would redeploy the
# release it was rolling back from. Clearing both here is what lets deploy.sh's
# own exports decide which images each step pulls.
prod-deploy:
	@test -n "$(IMAGE_TAG)" || { \
		echo "usage: make prod-deploy IMAGE_TAG=<40-character git sha>"; exit 1; }
	env -u IMAGE_TAG MAKEFLAGS= bash infra/deploy/deploy.sh $(IMAGE_TAG)

# No argument: the release to go back to is the one recorded in
# .release/previous, and a rollback that took a SHA from whoever is typing at
# 02:00 would be a rollback to whatever they remembered.
prod-rollback:
	env -u IMAGE_TAG MAKEFLAGS= bash infra/deploy/deploy.sh --rollback

# The edge's own CA root, for trusting https://app.localtest.me in a browser
# (R-5) or with curl --cacert. The containers that need it read it from the
# shared volume instead.
prod-edge-ca: prod-env-guard
	@$(PROD_COMPOSE) exec -T edge cat /edge-ca/root.crt

prod-logs: prod-env-guard
	$(PROD_COMPOSE) logs --tail=200 edge web api model-server feature-server \
		keycloak pgbouncer postgres-app postgres-keycloak redis

prod-down: prod-env-guard
	$(PROD_COMPOSE_ALL) down --remove-orphans

# Destructive to movielens-prod only. The whole point of the rehearsal is that
# the release sequence works from empty volumes with no manual priming, so this
# is the state every rehearsal run starts from.
prod-reset: prod-env-guard
	$(PROD_COMPOSE_ALL) down --volumes --remove-orphans
	$(MAKE) up-prod

# --- Staging ----------------------------------------------------------------
# One environment removed from production: the same compose file, the same
# images, the same release order, a different project and a different set of
# secrets. What it is for is rehearsing a release before `main` auto-deploys.
# What it is not is a second production -- there is no staging deploy workflow,
# no staging canary and no scheduled staging backup, and the absence of each is
# deliberate. See docs/deployment-runbook.md's "Staging" section.
#
# The usual sequence on a laptop:
#
#   cp infra/deploy/staging.env.example .env.staging   # then fill it in
#   make up-staging        # build the images from this checkout, start the stores
#   make staging-release   # roles, realms, migrations, seed, materialization
#   make staging-serve     # the serving tier
#   make staging-verify    # the post-deploy matrix
#
# On a staging host that pulls from GHCR, `make staging-pull IMAGE_TAG=<sha>`
# replaces the build step in `up-staging`.

# Same shape and same reason as prod-env-guard: every secret is generated, so
# there is no default file to fall back on, and failing here beats failing four
# services later on an interpolation error.
staging-env-guard:
	@test -f $(STAGING_ENV_FILE) || { \
		echo "$(STAGING_ENV_FILE) is missing."; \
		echo "  cp infra/deploy/staging.env.example $(STAGING_ENV_FILE)"; \
		echo "  then replace every REPLACE_ME__ value with:"; \
		echo "    python -c \"import secrets; print(secrets.token_urlsafe(48))\""; \
		exit 1; }

# The laptop entry point, mirroring up-prod: build every image from this
# checkout, then bring the stores up. A staging host that pulls published images
# runs `staging-pull` instead of this.
up-staging: staging-env-guard
	$(STAGING_COMPOSE_ALL) build
	$(MAKE) staging-stores

# The data tier and identity, with the one-time role provisioning in between --
# the release jobs cannot run until Postgres has the roles migration 0001
# expects and pgBouncer can authenticate against them.
staging-stores: staging-env-guard
	$(STAGING_COMPOSE) up -d --wait --wait-timeout 240 postgres-app postgres-keycloak redis edge
	$(STAGING_COMPOSE) run --rm -T postgres-provision
	$(STAGING_COMPOSE) up -d --wait --wait-timeout 300 pgbouncer keycloak

# Every image at whatever IMAGE_TAG is in the environment, then the assertion
# that they are all here: without it a failed pull would be quietly repaired by
# `up` building from whatever checkout this machine happens to have, which is
# the one thing a rehearsal of a published artifact must not do.
staging-pull: staging-env-guard
	$(STAGING_COMPOSE_ALL) pull
	@for image in $$($(STAGING_COMPOSE_ALL) config --images | sort -u); do \
		docker image inspect "$$image" >/dev/null 2>&1 || { \
			echo "missing after pull: $$image"; exit 1; }; \
	done
	@echo "all images present at IMAGE_TAG=$${IMAGE_TAG:-main}"

# Everything a release does to state and nothing that serves traffic, in the
# order the steps depend on each other: roles, realms, migrations, seed, feature
# materialization. The same order prod-release runs, because rehearsing a
# different order rehearses nothing.
staging-release: staging-env-guard
	$(MAKE) staging-stores
	$(STAGING_COMPOSE) run --rm -T keycloak-provision
	$(STAGING_COMPOSE) run --rm -T release
	$(STAGING_COMPOSE) run --rm -T materialize

# The serving tier. `--wait` holds until every container is healthy, which for
# the model sidecar means warm rather than merely listening.
staging-serve: staging-env-guard
	$(STAGING_COMPOSE) up -d --wait --wait-timeout 300 feature-server model-server api web

# The post-deploy matrix, then the non-latency reliability suite -- the same two
# stages prod-verify runs, with the same cooldown between them, because both
# authenticate as `verify` and share one ADR 0014 token bucket.
staging-verify: staging-env-guard
	$(STAGING_COMPOSE) run --rm -T verify
	@echo "waiting $(PROD_VERIFY_COOLDOWN_SECONDS)s for the verify subject's rate-limit bucket to refill"
	@sleep $(PROD_VERIFY_COOLDOWN_SECONDS)
	$(STAGING_COMPOSE) run --rm -T verify sh -c \
		'exec python -m synthetic.load.reliability \
			--api-url "$$API_URL" --keycloak-url "$$KEYCLOAK_URL" \
			--realm "$$VERIFY_REALM" --client-id "$$VERIFY_CLIENT_ID" \
			--client-secret "$$VERIFY_CLIENT_SECRET" \
			--username "$$VERIFY_USERNAME" --password "$$VERIFY_PASSWORD"'

# The edge's own CA root. More necessary here than in production: staging
# defaults to EDGE_TLS=internal, so a browser or a curl reaching the staging
# hostnames needs this to trust anything the edge serves.
staging-edge-ca: staging-env-guard
	@$(STAGING_COMPOSE) exec -T edge cat /edge-ca/root.crt

staging-logs: staging-env-guard
	$(STAGING_COMPOSE) logs --tail=200 edge web api model-server feature-server \
		keycloak pgbouncer postgres-app postgres-keycloak redis

staging-down: staging-env-guard
	$(STAGING_COMPOSE_ALL) down --remove-orphans

# Destructive to movielens-staging only -- different project, different volumes
# from both movielens-prod and movielens-demo. This is the state every staging
# rehearsal should start from: the release sequence has to work from empty
# volumes with no manual priming, and a stack that was hand-repaired once no
# longer proves that.
staging-reset: staging-env-guard
	$(STAGING_COMPOSE_ALL) down --volumes --remove-orphans
	$(MAKE) up-staging

# --- Keycloak realms --------------------------------------------------------
# Dumps the current live realm state (from the running Keycloak container)
# to infra/keycloak/realms/*.json so any changes made via the admin UI can
# be committed. See ADR 0007's realm-drift mitigation.
keycloak-export-realms:
	@echo "Exporting live realms to infra/keycloak/realms/ ..."
	docker compose exec -T keycloak /opt/keycloak/bin/kc.sh export \
		--dir /opt/keycloak/data/import \
		--realm default \
		--users realm_file
	docker compose exec -T keycloak /opt/keycloak/bin/kc.sh export \
		--dir /opt/keycloak/data/import \
		--realm demo \
		--users realm_file
	@echo "Done. Diff infra/keycloak/realms/ and commit any changes."
