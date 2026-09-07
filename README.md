# MovieLens Two-Stage Recommender

A multi-tenant, authenticated two-stage recommender on MovieLens 25M: item-item retrieval into a LightGBM ranker, served by FastAPI behind Keycloak and PostgreSQL row-level security, with a k6-gated p99 and a Next.js product on top of the same API any other client would use.

[![CI](https://github.com/kudratsingh/MovieLens-RecSys/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/kudratsingh/MovieLens-RecSys/actions/workflows/ci.yml)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![TypeScript 5 · Next.js 16](https://img.shields.io/badge/typescript%205-next.js%2016-black)
[![license: all rights reserved](https://img.shields.io/badge/license-all%20rights%20reserved-lightgrey)](#license)

<img alt="Discover at 1440px, signed in as Demo Walkthrough and exploring as the Drama Fan persona: a featured recommendation labelled RANKED BY THE LEARNED MODEL, rank 1, with Open movie, Watchlist, Mark watched and Not for me controls, the Why this? disclosure closed beneath them, and the ranked rail beginning below." src="docs/frontend/evidence/current/discover-desktop-1440.png" width="100%">

Two tracks share this repository, and the models are the reason it exists. The modeling track starts
from the baselines the field measures against and builds the two-stage architecture the way production
recommenders are built — item-item retrieval, a two-tower with FAISS, a LightGBM LambdaRank ranker today
— and keeps moving toward what the industry ships now: sequence models with transformer encoders are
the next step ([the modeling roadmap](docs/modeling-roadmap.md) is the ladder, rung by rung, each
needing approval before it starts). The engineering track is what makes those models usable by a real person rather than a
notebook: the tenant boundary the database enforces rather than the application, the feature-freshness
contract, the SHA-256-pinned serving bundle, the latency gate that measures the service instead of the
CI runner, and a serving contract that reports when the learned path did *not* run rather than quietly
taking credit for the fallback. Phase 3 is the engineering track's turn — the harness that makes the
product and the API real — and the modeling track resumes as the main line of work once it closes.
Every significant decision, model choices included, is written down before the code that depends on it
lands — sixteen ADRs, each with its alternatives analyzed and the signals that would reopen it.

## What is real today

**As of 2026-08-29.** Phases 1 and 2 are complete. Phase 3 is in progress, and this is what is on
`main` and runs locally through Docker Compose:

- **Authenticated serving.** Keycloak OIDC, one realm per tenant; the tenant comes from the token
  issuer, never from a client-declared claim. Every endpoint but `/healthz` and `/readyz` needs a token.
- **Tenant isolation the database enforces.** Forced row-level security on every tenant-scoped table,
  `SET LOCAL app.tenant_id` inside a per-request transaction, pgBouncer in transaction-pool mode so
  it cannot leak. The API refuses to boot if its engine can bypass RLS or the pooler is in session mode.
- **A learned two-stage path.** Item-item retrieval into a LightGBM LambdaRank booster, loaded once at
  startup from a SHA-256-pinned manifest by a private sidecar, reading features from Feast over Redis.
  Cold or unavailable paths fall back to popularity and *say so* in the response.
- **Durable prediction audits.** Ranked items, scores, online feature values, model versions,
  structured fallback reason and per-stage timings, committed before the response is sent.
- **Measured cold-start coverage.** A fixed-seed cohort of 2,000 synthetic users at history sizes
  0/1/3/10, scored per bucket by the same evaluation harness ([ADR 0011](docs/adr/0011-cold-start-coverage.md)).
  On its first run it falsified a claim: the offline candidate models fall back on "no history at
  all" rather than on ADR 0001's threshold, so a one-interaction user was handed item-item
  neighbours and did about a third as well as the fallback would have — reported, not patched.
- **The movie-discovery product.** Discover, Browse, movie detail, Library and Quick Picks behind one
  shell, ML evidence behind progressive disclosure. `/` serves it; the pre-redesign dashboard is
  retained at `/legacy` as a documented rollback.

**Nothing is deployed.** The production target is specified, built and rehearsed end to end — one
Hetzner CX22 running `docker-compose.prod.yml` behind its own Caddy edge, GHCR images tagged with the
commit SHA, an SSH deploy that rolls back automatically when post-deploy verification fails — and the
whole release sequence has been driven from empty volumes in production mode on a laptop, in a
[14-step rehearsal](docs/production-readiness-review.md) that earned its keep: it was the first boot
of this codebase with `ENVIRONMENT != dev`, and every defect it exposed was fixed in the same branch.
**The machine has not been created, so there is no URL**, and the deploy and canary workflows stay
green no-ops until one is configured. See [ADR 0013](docs/adr/0013-production-deployment-target.md)
and the [deployment runbook](docs/deployment-runbook.md).

Still open in Phase 3: audit retention, and the ranker's training feature source —
deferred as D-009 rather than decided (below). Per-tenant champion routing and the
offline routing gap the cold-start cohort found have both since landed. The dev and staging Compose
environments landed (`make up-dev`, `make up-staging`); staging is a thin overlay on the production
stack and, like production, has no host yet. The frontend finish gate passes every
criterion a reviewer can settle and [holds](docs/frontend/finish-gate-review.md) on moderated
sessions with real participants, which a reviewer cannot substitute for. The long form is in
[`docs/status/`](docs/status/README.md) and [`docs/README.md`](docs/README.md).

## Quickstart

Docker Desktop or another Docker Engine with Compose v2. Ports 3001, 5432, 6379, 6432, 8000 and 8080
must be free. Python and Node are not needed on the host. The first start downloads base images and
builds the application images, and is substantially slower than later cached starts.

```bash
cp .env.example .env
make demo-up
make demo-seed
make demo-smoke
```

Open <http://localhost:3001> and sign in as **`demo` / `demo`** — a dev-only account seeded from
`infra/keycloak/realms/demo-realm.json`, labelled as such in the file and not a secret. The demo
stack runs with the auth bypass disabled, so this is the real authorization-code + PKCE flow.

It runs an isolated Compose project against the full 62,423-title MovieLens catalog, seeded from a
committed snapshot with 120 reviewed titles postered on top, so the 25M dataset is not required. Warm personas are served the `item-item-cosine+lightgbm` policy with checksum-pinned model
versions; the Cold Start persona gets the explicit `popularity` fallback. `make demo-audits` prints
the newest durable audit rows — the exact predictions, features, versions and stage timings behind
what you just saw. TMDB posters are optional: set `TMDB_READ_ACCESS_TOKEN` in `.env` first, or leave
it empty for generated artwork. Walkthrough, reset and troubleshooting:
[`docs/demo-runbook.md`](docs/demo-runbook.md).

## Architecture

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/diagrams/online-request-path.dark.svg">
  <img alt="One authenticated GET /users/{id}/recommendations as a sequence: request-id adoption, token verification and tenant derivation, the rate-limit bucket, the movie-state read, the cold-start branch versus the learned two-stage path through the sidecar, the audit insert, and the commit that precedes the response." src="docs/diagrams/online-request-path.svg" width="100%">
</picture>

**Online.** The tenant comes from the issuer that signed the token; the audience and authorized party
must be on an allow-list. A per-request transaction opens with `SET LOCAL app.tenant_id`, and one
read of `user_movie_state` returns positives and exclusions separately. Below ten positive signals
the request takes the popularity fallback. Above it, the private sidecar retrieves item-item
candidates — only dismissals may drop a seed — batch-reads eight features per candidate from Redis,
and scores them with LightGBM. Exclusions are re-applied at retrieval, at hydration, and in a final
fail-closed sweep. The audit row commits *before* the response goes out, so there is no 2xx for a
prediction that could still fail to become durable.

**Offline.** MovieLens 25M into Postgres, a temporal split, point-in-time features, both model
stages, then a SHA-256-pinned manifest binding the index, the booster, the tenant and the ordered
feature contract. That bundle is baked into the sidecar image, so rolling back the model is rolling
back the image. The full walkthrough — with the tenancy, data-model, topology and CI diagrams — is in
[`docs/architecture.md`](docs/architecture.md).

## Measured

Latency is a gate, not a claim. The pinned k6 profile drives real Keycloak-authenticated warm, cold
and mixed traffic for 60 seconds and fails the build on **p99 ≥ 100 ms, any request error, any
response that is not correct, or fewer than 50 requests/second**. The thresholds have never moved.

| Measurement | When | Result |
|---|---|---|
| Accepted CI baseline | 2026-08-20 | p50 6.31 ms · p95 14.27 ms · p99 41.30 ms, 54.08 req/s over 3,301 requests, zero errors |
| Native thread teams pinned to 1 in the model server ([PR #69](https://github.com/kudratsingh/MovieLens-RecSys/pull/69)) | 2026-08-27 | p99 903.64 ms → **48.99 ms** at 0% host CPU steal, on the same unchanged gate |
| CI Postgres data directory moved to tmpfs ([PR #80](https://github.com/kudratsingh/MovieLens-RecSys/pull/80)) | 2026-08-28 | p99 230.74 ms → **24.41 ms**; WAL sync 3.15 → 0.21 ms per commit |
| The same gate at the production topology | 2026-08-27 | p50 6.85 ms · p95 9.47 ms · p99 12.93 ms, zero errors, zero dropped iterations |
| Rate limiter at its first defaults (120/min, burst 30) | 2026-08-27 | **37.9% of 301 requests refused** — keep-alive pins one client to one worker's bucket; defaults are now 600/min, burst 120 |
| The bucket moved into Redis, shared by every worker ([issue #70](https://github.com/kudratsingh/MovieLens-RecSys/issues/70)) | 2026-08-29 | One atomic `EVALSHA` per authenticated request; the limit now describes the service rather than one process, and an unreachable Redis costs **0.2 ms** and falls back rather than the 6.7 s redis-py's default retries spent first |

Sources: [ADR 0010](docs/adr/0010-synthetic-load-k6.md) (the first three),
[the readiness review's R-14](docs/production-readiness-review.md), and
[ADR 0014](docs/adr/0014-request-rate-limiting.md). Rows two and three are worth reading in full: in
both, the number moved because the *measurement* was wrong rather than the service, and the
resolution was to take the runner out of the measured path — never to relax a threshold.

**The data.** 25,000,095 ratings, 162,541 users, 59,047 movies ever rated (62,423 in catalog),
sparsity 0.2605%. The split is temporal and fixed: `T = percentile_disc(0.8)` lands on
2016-06-25 06:49:57 UTC, train is 20,000,075 rows (80.00% on the nose), holdout is 129,683 rows
across 28 days and 2,641 users, and the remaining 19.48% is reserved as test.
[`docs/eda.md`](docs/eda.md) has the distributions and what the long tail means for cold start.

**Offline model metrics.** `src/evaluation/` is the single source of truth — recall@500 for the
candidate stage, NDCG@10 for the ranker, sliced warm and cold per
[ADR 0001](docs/adr/0001-evaluation-protocol.md), with `EvalResult.k` stamped on every result so a
candidate-stage number can never be read as an end-to-end one. The table is one run of each trainer
on the full 25M rows on 2026-08-29 (single seed, one machine); every figure with its run, wall-clock
and caveats is in [`docs/results.md`](docs/results.md). Holdout: 1,939 warm users, 702 cold.

| Stage | Model | Warm recall / NDCG | Cold recall / NDCG | Overall recall / NDCG |
|---|---|---:|---:|---:|
| Baseline, K = 10 | Popularity | 0.0163 / 0.0309 | 0.0638 / 0.4881 | 0.0290 / 0.1525 |
| Baseline, K = 10 | CF / ALS | 0.0338 / 0.0578 | 0.0638 / 0.4880 | 0.0418 / 0.1722 |
| Candidates, K = 500 | Item-item cosine | 0.4001 / 0.1392 | 0.5290 / 0.4392 | 0.4344 / 0.2190 |
| Ranker, K = 10 | LightGBM LambdaRank | 0.0394 / 0.0554 | 0.0793 / 0.5631 | 0.0500 / 0.1904 |

Three things the table says that a leaderboard would not. Cold NDCG@10 dwarfs warm for every model
because cold users rate the canonical popular titles — which is why per-policy attribution exists.
On this single run the ranker lifted warm recall@10 by 16.5% over CF/ALS while its warm NDCG@10
*fell* 4.2% — **and that fall turned out to be noise.** Re-seeding showed the ranker's warm NDCG@10
moving 28.7% of its own mean on the seed alone, because it was training on 20,000 positives sampled
from a 154,003-row window; trained on the whole window it beats CF/ALS by **+21.2% warm and +15.5%
overall, seed-averaged and at every individual seed**, and
[ADR 0001](docs/adr/0001-evaluation-protocol.md)'s gate — which since 2026-08-30 reads overall
NDCG@10 at +3% with a measured per-slice non-regression clause — promotes it. Item-item's top-500
still holds only 57.0% of the ranker's training positives. The two-tower has since run to
completion and lost — warm recall@500 **0.0466** against item-item's 0.4001, and against **0.2310**
for the popularity list it carries inside itself as its own cold-start fallback — so
[ADR 0004](docs/adr/0004-item-item-before-two-tower.md)'s gate is not cleared and item-item stays
champion. A [learning-rate and budget sweep](docs/results.md#2026-08-30-fourth-session--the-two-towers-learning-rate-and-budget-swept)
of 12 pilot cells and two full-dataset runs closed the hyper-parameter explanation rather than the
model: the learning rate was already right to within a decade either side, eight epochs score below
three, and v1's flat loss curve was not convergence but a model with no range left to speak in —
its final loss of 10.2718 is worse than `ln(1 + 16,384) = 9.7041`, the loss of a model that emits
the identical logit for every candidate. On the synthetic cold-start cohort,
item-item at history sizes 0/1/3/10 scores recall@500 0.4760 / 0.1440 / 0.2880 / 0.3900 with
`synth_cold_routing_ok = false`
([ADR 0011, 2026-08-29 note](docs/adr/0011-cold-start-coverage.md)).

## What makes this not a toy

Eleven bright-line rules, most of them enforced by a named CI job rather than by intention. The
[full list](CLAUDE.md) is in the project brief; these are the ones with mechanisms behind them:

| Job | What it proves |
|---|---|
| `feature-parity` | An offline-computed feature equals the online-served feature for the same key, against live Postgres and Redis. This is the bug that ruins most real recsys deployments. |
| `tenant-isolation` | Authenticates as tenant A against real Keycloak and RLS, fires every endpoint, and asserts no tenant B data surfaces. Cross-tenant leakage is the highest-severity bug class here. |
| `synthetic-load-smoke` | The 60-second k6 gate above, plus page-shaped per-step budgets and the non-latency reliability checks. Runs the job's Postgres on tmpfs, because every request commits an audit row and the runner's block device would otherwise sit inside every percentile it measures. |
| `serving-artifacts` | Rebuilds the serving bundle inside the image and diffs it against the committed one, so a training change that would silently move an artifact hash fails the build. |
| `realm-drift` | Exports both Keycloak realms from a fresh volume and diffs them against the committed seeds. |
| `browser-auth-e2e` | Real PKCE browser journeys with the dev bypass disabled, then LCP, CLS and time-to-acknowledgement in a throttled mobile browser. |
| `demo-compose` | Validates the demo and production Compose models, and greps the rendered production model to assert `DEV_AUTH_BYPASS` appears nowhere in it. |
| `lint` / `frontend` | Ruff, Black, strict mypy, and the two contract drift checks: the committed OpenAPI document and the TypeScript types generated from it. |

`main` is protected: pull requests are required, force-pushes and deletions are blocked, and
`lint`, `test`, `feature-parity`, `tenant-isolation`, `synthetic-load-smoke`, `demo-compose`,
`frontend` and `browser-auth-e2e` are required status checks. Images publish to GHCR only after every
job is green.

## Product

<img alt="Browse at 390px: catalog search, a filters control, a sort selector reading Most watched here, a 24 titles loaded count, the poster grid, and the mobile bottom navigation." src="docs/frontend/evidence/current/browse-mobile-390.png" height="250"> <img alt="The movie detail page for The Matrix: tagline, runtime, genres, TMDB score, synopsis, the Watchlist / Watched / Not for me controls, and a rating of 5 out of 5 captioned as display feedback rather than a graded training signal." src="docs/frontend/evidence/current/movie-detail-desktop-1440.png" height="250"> <img alt="The Why this? disclosure open on Discover, showing the prediction audit: policy item-item-cosine+lightgbm, the learned-two-stage reason over 8 positive seeds, the filter policy, candidate sources, pinned candidate, ranker and feature versions, the feature event time, the per-stage latency breakdown, the rank score, and the online feature values." src="docs/frontend/evidence/current/discover-why-this-desktop-1440.png" height="250">

The frontend is not an end-user product; it is the surface that makes the engineering visible.
**Discover** loads recommendations and watch history as independent regions, so a failed resource
never blanks the ones around it, and the policy label follows what the response reported rather than
an inference. **Browse** sits on a cursor-paginated catalog contract with URL-owned filters and
cursors bound to the query fingerprint — reuse one against a different query and it is a 400, not a
wrong page. The **movie page** carries TMDB detail over a local read model, so a poster grid never
fans out to TMDB per card. **Library** reads Rated, Watchlist and Seen independently, with optimistic
writes reconciled against the committed revision. **Quick Picks** advances only after the API
commits, because a one-card queue that rolled back would re-show a title the viewer already
dismissed. Throughout, the ML evidence — prediction audit, online feature values, the uncalibrated
rank score — is two deliberate actions away, never in the way of the first movie.

Product docs, design contracts and the testing strategy are in
[`docs/frontend/`](docs/frontend/README.md); the
[finish-gate review](docs/frontend/finish-gate-review.md) records the verdict per criterion and what
is still owed.

## Design decisions

| # | Decision |
|---|---|
| [0001](docs/adr/0001-evaluation-protocol.md) | Temporal split, recall/NDCG@10 reported warm and cold separately, +3% relative promotion gate |
| [0002](docs/adr/0002-implicit-feedback-label.md) | Every rating is a positive interaction; the rating value leaves the modeling pipeline |
| [0003](docs/adr/0003-two-stage-architecture.md) | Two stages, because single-model global scoring misses the p99 SLO by orders of magnitude |
| [0004](docs/adr/0004-item-item-before-two-tower.md) | Item-item first, as the zero-learned-parameters baseline the two-tower must beat |
| [0005](docs/adr/0005-lightgbm-over-neural-ranker.md) | LambdaRank GBDT over a neural ranker; tabular point-in-time features are GBDT's home turf |
| [0006](docs/adr/0006-two-tower-retrieval-architecture.md) | History-based two-tower, sampled softmax with log-uniform correction, FAISS IVF-Flat |
| [0007](docs/adr/0007-auth-provider-keycloak.md) | Self-hosted Keycloak, realm per tenant; tenant from the issuer, never from a claim |
| [0008](docs/adr/0008-multi-tenancy-rls.md) | Forced Postgres row-level security, `SET LOCAL` per request, pgBouncer in transaction mode |
| [0009](docs/adr/0009-feature-store-feast.md) | Feast with Postgres offline and Redis online; `tenant_id` a join key on every feature view |
| [0010](docs/adr/0010-synthetic-load-k6.md) | k6 as the SLO's only authority, with a measurement-validity rule instead of a movable threshold |
| [0011](docs/adr/0011-cold-start-coverage.md) | Fixed-seed synthetic cohorts at history sizes 0/1/3/10, scored per bucket |
| [0012](docs/adr/0012-browser-identity-feedback-and-online-freshness.md) | Actor separated from demo persona, durable movie state, commit before acknowledgement |
| [0013](docs/adr/0013-production-deployment-target.md) | One Hetzner CX22 running the same Compose file the rehearsal runs; cost is the deciding factor and says so |
| [0014](docs/adr/0014-request-rate-limiting.md) | Per-`(tenant, subject)` token bucket on the verified token, never on a client IP; one bucket in Redis for every worker, failing open onto a per-worker one |
| [frontend/0001](docs/adr/frontend/0001-frontend-framework.md) | Next.js App Router with TypeScript and Tailwind |
| [frontend/0002](docs/adr/frontend/0002-movie-discovery-experience.md) | Poster-first discovery with ML evidence behind progressive disclosure |

[`docs/adr/README.md`](docs/adr/README.md) has the one-line decision for each, the status notes, and a
four-ADR reading order for a reviewer in a hurry.

## Stack

| Layer | Choice |
|---|---|
| Models | PyTorch (two-tower), LightGBM LambdaRank (ranker), `implicit` (ALS, cosine item-item) |
| ANN retrieval | FAISS-CPU, IVF-Flat over cosine-normalized item embeddings |
| Data store | PostgreSQL, with Alembic migrations and pgBouncer in transaction-pool mode |
| Reproducibility | DVC for data, MLflow for experiments and the registry |
| Feature store | Feast — Postgres offline, Redis online |
| Serving | FastAPI + Redis, with a private model sidecar |
| Auth | Keycloak OIDC, one realm per tenant |
| Tenant isolation | PostgreSQL forced row-level security |
| Frontend | Next.js 16 + TypeScript 5 + Tailwind; Vitest, Playwright, jest-axe |
| Load + reliability | k6 (pinned), with page-shaped budgets and a browser timing suite |
| Planned | Prefect orchestration (Phase 4), Evidently drift detection (Phase 5); Prometheus + Grafana are in the dev Compose stack, and nothing exports `/metrics` yet ([ADR 0013](docs/adr/0013-production-deployment-target.md)) |
| CI/CD | GitHub Actions — twelve jobs, GHCR `linux/amd64` images tagged with the commit SHA |
| Hosting (specified, not provisioned) | One Hetzner CX22 behind Caddy; SSH deploy with automatic rollback |

## Repository map

```
src/            auth/ · data/ · evaluation/ · features/ · models/ · serving/ · training/ · release/
alembic/        fourteen migrations: tenant roles, tenant_id, forced RLS, movie state, audits
synthetic/      load/ (the k6 gate) · personas/ · smoke/ · tenant_isolation/   → synthetic/README.md
web/            Next.js app, the one resource boundary, the one write path, four test layers
tests/          unit/ · feature_parity/ · learned_serving/ · tenant_isolation/ · integration/
infra/          images, Keycloak realms, pgBouncer, Caddy edge, host bootstrap  → infra/README.md
docs/           architecture, ADRs, API contract, frontend, runbooks, EDA      → docs/README.md
```

## Documentation

Read in this order:

1. [`docs/architecture.md`](docs/architecture.md) — the system on one page, with ten rendered diagrams
2. [`docs/modeling-roadmap.md`](docs/modeling-roadmap.md) — the model ladder: where the models are, the rungs from here to a Netflix-class stack, which need approval next
3. [`docs/adr/README.md`](docs/adr/README.md) — every decision, its alternatives, and what would reopen it
4. [`docs/api/overview.md`](docs/api/overview.md) — every endpoint, and the committed OpenAPI contract beside it
5. [`docs/demo-runbook.md`](docs/demo-runbook.md) — running the whole stack from a clean checkout
6. [`docs/frontend/README.md`](docs/frontend/README.md) — the product docs, and the [finish gate](docs/frontend/finish-gate-review.md)
7. [`docs/production-readiness-review.md`](docs/production-readiness-review.md) — the gap review and the 14-step rehearsal record
8. [`docs/deployment-runbook.md`](docs/deployment-runbook.md) — the machine, DNS, secrets, first deploy, rollback, backups
9. [`docs/eda.md`](docs/eda.md) — MovieLens 25M: scale, sparsity, the long tail, the split as applied
10. [`docs/records/`](docs/records/) — dated documents kept for their reasoning and **not maintained**

## Development

```bash
make install          # Python deps
make infra-up         # Postgres, pgBouncer, Redis, Keycloak, MLflow, Prometheus, Grafana
make db-migrate       # alembic upgrade head

make lint             # ruff + black --check
make typecheck        # mypy, strict, on src/ synthetic/ notebooks/
make test             # pytest (make test-unit for the fast subset)
make serve            # uvicorn on :8000 with reload

make web-install      # then: make web-dev (:3001), web-test, web-e2e
make diagrams         # re-render docs/diagrams/ from source
```

Training entrypoints are `make train-popularity`, `train-cf`, `train-itemitem`, `train-twotower` and
`train-ranker`; each logs to its MLflow experiment (UI on <http://localhost:5000>).
`make data-download` and `make data-ingest` fetch and load the full 25M dataset, which the demo does
not need. Work goes on short-lived branches and merges by pull request — never a direct push to
`main` — with [Conventional Commits](https://www.conventionalcommits.org/) and a filled-in
[pull request template](.github/PULL_REQUEST_TEMPLATE.md).

## Security

Keycloak, JWT validation, RLS as the tenant boundary and a rate limiter are all real machinery here,
and a report against any of it is welcome. The disclosure process, the guarantees the project
considers highest-severity, and an explicit note on the dev-only credentials committed to this
repository are in [`SECURITY.md`](SECURITY.md).

## License

Published for portfolio visibility. No open-source license is granted; all rights reserved.
