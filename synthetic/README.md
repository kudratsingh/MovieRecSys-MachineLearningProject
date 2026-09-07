# Synthetic users and harnesses

Four harnesses live here, each with a narrow job. They are not one
"synthetic data generator" with modes — that framing produces a tool nobody
trusts for any single purpose. What they have in common is that they all
authenticate the way a real client does, against real Keycloak, and that none of
them is allowed to prove something by not running.

This directory ships **inside the API image**, which is why the deployable
cross-tenant probe lives here rather than under `tests/` — the image copies
`synthetic/` and never `tests/`.

| Directory | Job | Runs via |
|---|---|---|
| [`load/`](#load--the-latency-gate-and-its-evidence) | Measure the p99 SLO, and check the serving promises a percentile cannot express | `make demo-load-smoke`, `demo-load-pages`, `demo-reliability-check`, `prod-load` |
| [`personas/`](#personas--the-four-demo-identities) | Four named demo identities, the reviewed 120-title catalog they explore, and the full 62,423-title MovieLens catalog underneath it | `make demo-seed`, `make catalog-verify` |
| [`smoke/`](#smoke--is-this-deployment-actually-working) | Readiness and behavioural checks against a deployed stack | `make demo-smoke`, `make demo-audits` |
| [`tenant_isolation/`](#tenant_isolation--the-deployable-cross-tenant-probe) | Prove cross-tenant refusal against a running deployment | `make prod-verify` (as check V-6) |

Planned or deferred: `cold_start/` (ADR 0011's fixed-seed cohort — being added on
a parallel branch), `drift/` (Phase 5), `ab_fixtures/` (Phase 6).

---

## `load/` — the latency gate and its evidence

Non-negotiables #4 and #11 say the p99 SLO is *measured under synthetic load*,
not assumed, and that CI enforces it on every serving PR. This directory is that
measurement, plus the machinery that makes a failure interpretable.

**`thresholds.js` is the SLO's only authority.** Six lines, no comments, four
thresholds tagged `{endpoint:recommendations}`:

```js
"checks{endpoint:recommendations}":            ["rate==1"],
"http_req_duration{endpoint:recommendations}": ["p(99)<100"],
"http_req_failed{endpoint:recommendations}":   ["rate==0"],
"http_reqs{endpoint:recommendations}":         ["rate>50"],
```

p99 under 100 ms, zero errors, every check passing, and more than 50
requests/second actually achieved. The other two threshold modules are separate
files on purpose, so a weaker budget cannot be edited into the gate by accident
and `git log thresholds.js` stays a truthful record that the numbers have never
moved.

| File | What it does |
|---|---|
| `recommendations.js` | The gated workload: constant-arrival authenticated traffic over three warm personas and one cold one. Three profiles — `smoke` (60 s at 55/s, the CI gate), `nightly` (5 min at 600/s), `prod-canary` (60 s at 5/s). Beyond HTTP 200 it asserts the policy matches and that a **warm persona actually reports the learned path**, because a persona that quietly degraded to popularity is fast and wrong |
| `thresholds.js` | The pinned gate above |
| `canary_thresholds.js` | The post-deploy canary's deliberately weaker contract: checks pass, zero failures, and no latency verdict — 5 arrivals/second across a provider's network is not comparable to the accepted baseline |
| `pages.js` | Page-shaped workloads modelling each route's real fan-out, read off `web/lib/**` rather than guessed: Discover's three concurrent calls, Browse's cursor walk, Library, a mutation-plus-immediate-read, and the Quick Picks action sequence. Refuses to start if pointed at the Cold Start persona |
| `page_thresholds.js` | Those budgets, split into always-enforced correctness and separately-gated latency. Derivation is written down: `1.5×` the worst value at least two runs corroborate, rounded up to the next 10 ms |
| `run_gate.sh` | Runs one window, collects the evidence around it, and applies exactly one rule — **re-measure if and only if latency breached *and* the breaching seconds line up with host preemption**. A breach with no preemption under it fails immediately; a re-measured window is final, so it cannot loop |
| `summarize.py` | Turns a window into a per-second latency table joined against `/proc/stat`. Owns the re-measure constants (≥3 of the 10 slowest seconds at ≥10% steal) and refuses to apply them to a correctness breach — preemption cannot make an API return the wrong body |
| `reliability.py` | Ten pass/fail serving promises (below) |
| `probe_host_cpu.py` | Samples steal and run-queue depth each second, so an unexplained tail becomes evidence for or against preemption |
| `probe_disk_fsync.py` | Times `fdatasync` on Postgres's data volume, since every request commits an audit row. Informational, and opt-in: its own docstring records the run where continuous sampling moved p95 4.5× and failed the gate |
| `server_side.py` | Exports the window's audit rows and Postgres WAL/IO counters. The point is the *difference* — a p99 of 40 ms at the client and 8 ms in the handler puts 32 ms outside the handler |
| `lib/auth.js`, `lib/stack.js` | Keycloak password grant with re-mint before expiry; readiness polling and UUID generation |

**`reliability.py`** answers what an operator asks at 3 a.m. and a percentile
cannot: `/healthz` reachable without a token while every other route answers
401; a caller's `X-Request-ID` echoed *and* persisted to the audit row's
`correlation_id`; a minted one when the header is malformed; auth, model and
database provenance readable from `/whoami` and the audit; bounded page sizes; a
cursor reused under a different filter refused; degraded metadata rendering as a
record rather than a failure; and rate limiting.

The rate-limit check is required and asserts the contract a client codes
against — `X-RateLimit-*` on an admitted response, and a `429` with
`Retry-After`, `X-RateLimit-Remaining: 0` and a JSON `detail` once the bucket is
drained, with no third outcome. Since the shared bucket landed (ADR 0014's
2026-08-29 note) it also asserts there is exactly *one* bucket behind the
service rather than one per worker: no more than one bucket's worth admitted
before the first refusal, and a concurrent burst of brand-new connections
refused afterwards, because a reconnect that buys a fresh allowance is what a
per-worker bucket looks like from outside. Both bounds are arithmetic against
the capacity and refill the `429` itself advertised, so a deployment that
changes `RATE_LIMIT_*` changes nothing here; `--rate-limit-probe-requests` and
`--rate-limit-shared-probe-connections` are the two knobs for a target that
needs a wider window or no reconnect sweep.

What the check cannot assert is that a limiter is *configured*: ADR 0014 turns
the bucket off on a dev stack precisely so the harnesses here can drive one
Keycloak identity past any sane per-subject rate. Against that target the check
records the absence in words and names the fact that every deployed environment
should show the enforced branch instead.

The degraded-metadata check used to be the one place a `skipped` was correct:
with a 120/120 postered catalog it scanned ten pages, found no poster-less title,
and reported why rather than inventing one. The full MovieLens catalog under the
reviewed 120 hands it 62,303 of them, so it now exercises the branch it was
written for on every run. The degraded rendering itself is held by
`web/e2e/poster-fallback.spec.ts`.

## `personas/` — the four demo identities

Four tenant-scoped synthetic users in the `demo` tenant, chosen so their
recommendations are visibly different from one another:

| Persona | ID | History |
|---|---|---|
| Action Fan | 900000101 | 12 |
| Drama Fan | 900000102 | 12 |
| Eclectic Viewer | 900000103 | 11 |
| Cold Start | 900000104 | **0** — served by the explicit popularity fallback |

The three warm histories all clear ADR 0001's `COLD_START_THRESHOLD` (10 since
2026-08-30), which is what `make demo-smoke`, the k6 gate's `learned` assertion,
`src/release/verify.py` V-5 and the browser journeys all read back. Action Fan
and Drama Fan carry a margin of two over the boundary so a journey that
dismisses a title cannot tip them onto the fallback mid-run.

`catalog.json` holds the reviewed 120-title catalog; every title carries a
poster URL, an overview, and a `details` object. `personas.json` holds the four
identities and their histories. `seed.py` is idempotent — deterministic ids via
`uuid5`, a pinned base timestamp — so re-seeding produces the same rows.

`movielens-catalog.csv.gz` is the floor those 120 titles sit on: all 62,423
MovieLens-25M titles with their genres and TMDB ids, and nothing else.
A retriever fitted on the full dataset ranks ids from a 34,461-item vocabulary,
and while the demo database held only the reviewed titles none of those ids were
rows — hydration returned nothing and the API answered every request with the
popularity fallback, correctly and uselessly. The snapshot is committed rather
than read from `data/raw/ml-25m` at seed time because the seeder runs inside the
API image, which copies `synthetic/` and never `data/`, and because a catalog
that appeared only on machines that had run `dvc pull` would make the
reproducibility gate that trains off this database machine-dependent.
`build_movielens_catalog.py` regenerates it; the reviewed fixture is written
first and wins every column it owns, so a bulk load can never revert an
editorial title or a trimmed genre string.

Only the reviewed 120 carry artwork. Enriching 62k titles is a TMDB budget this
project has no reason to spend, so Browse outside them renders the documented
placeholder — sparser than before, not richer, and deliberately so.

`enrich_posters.py` and `enrich_details.py` are **offline** passes that write
TMDB data into the fixture, which is what keeps the request path from fanning
out to TMDB per card. Both are idempotent to the byte, both check before they
write (a poster URL is HEADed before it lands), and an existing overview is only
ever filled, never replaced — an automated pass has no business overwriting an
editorial choice. `make catalog-verify` re-HEADs every stored URL; it is
deliberately **not** a CI gate, because a third party's uptime must not decide
whether a pull request is mergeable. The offline half — that every entry carries
a URL in the pinned shape — is a unit test and does gate CI.

Cold Start is load-bearing rather than decorative. Several suites here refuse to
run, or to finish, if anything has pushed it past the cold-start threshold: it
is the only persona that proves the fallback path, and a harness that quietly
warmed it would delete its own control.

## `smoke/` — is this deployment actually working

`demo.py` is GET-only and deployment-agnostic — realm, client and grant are
flags, so the same assertions can be pointed at a production realm. It checks
that FastAPI, Next.js and Keycloak are reachable, that the four personas are
discoverable, that Action Fan has history and recommendations with no seen-item
overlap and was served by a real retrieval family with the ranker behind it, and
that Cold Start has no history and reports `popularity`.

The warm assertion is on the *shape* of the policy the coordinator composed —
`<retrieval family>+lightgbm`, refusing `popularity`, `popularity-fill` and
`popularity-fallback` by name — rather than on one champion's spelling of it. It
used to require `item-item-cosine+lightgbm` literally, which meant a promotion to
any other champion could not pass its own smoke step: a SASRec bundle answers
`sasrec+lightgbm`. `--retriever-family` pins it exactly for a caller that knows
which family should be answering.

It also compares the API-served catalog metadata against the reviewed fixture,
because a stale snapshot is invisible to every other check and very visible to a
viewer: it is posters that never load. That walk sorts by popularity, and the
reason is arithmetic: only the reviewed titles carry seeded interactions, so
popularity puts all 120 in the first three pages, while the default title sort
over a 62,423-title catalog reaches 1 of them in eight pages and passes by never
looking at anything.

Modes: `--readiness-only` (what `make demo-up` ends with), `--audits-only`
(`make demo-audits`), and the full run (`make demo-smoke`).

## `tenant_isolation/` — the deployable cross-tenant probe

`remote_canary.py` is the deployment-side half of non-negotiable #9. It speaks
HTTP only, takes every identity as a flag, and proves two properties:

1. An authenticated actor from tenant A **without** the `demo-impersonator` role
   is refused on every persona-scoped route naming tenant B's persona ids —
   and **exactly `403`**. A `404` would also be a denial, but by persona scoping
   rather than by the guard, which is a right answer for the wrong reason, so it
   is reported as a failure.
2. No response body carries a `tenant_id` other than the caller's, anywhere in
   its payload. (A blanket "must not be 2xx" would be wrong: recommendations
   falls back to popularity for an unknown id, and history comes back empty.)

Two properties of the probe itself matter as much as the assertions. **An
unreachable target is a hard failure, never a skip** — a harness that reports
success because it could not reach the thing it verifies converts an outage into
a green check. And it is **safe to point at production**: the per-movie routes
name a movie id in no catalog, and the bulk rating reset names a user id that is
nobody's persona, so even a broken guard fails closed.

The CI-side counterpart, which runs against the real Compose stack with a
database, is `tests/tenant_isolation/`.

## Where these run

| Target | What it runs |
|---|---|
| `make demo-load-smoke` | The 60-second pinned gate. CI's `synthetic-load-smoke` job |
| `make demo-load-quiesce` | Stops the services the gate does not measure. Not optional on a shared runner |
| `make demo-load-pages` | The page-shaped workloads (~70 s more) |
| `make demo-load-nightly` / `-pages-nightly` | The longer profiles, locally |
| `make demo-reliability-check` | The ten reliability facts |
| `make demo-seed` | Personas and catalog |
| `make catalog-verify` | Re-HEADs every fixture poster URL |
| `make demo-smoke` / `demo-audits` | Behavioural smoke; newest audits |
| `make prod-verify` | The deploy verification matrix, which calls the remote canary as check V-6, then `reliability.py` with production identities |
| `make prod-load` | The advisory `prod-canary` k6 profile |

In CI, `synthetic-load-smoke` boots the demo stack, seeds it, quiesces the
unmeasured services, then runs the gate, the page workloads and the reliability
check against the same warm stack, uploading its evidence directory on every run
— a passing run is the baseline the next failure is compared against. That job
alone runs Postgres's data directory on tmpfs, because every request commits a
durable audit row and a rented runner's disk would otherwise sit inside the p99.
`lint` and `typecheck` cover this directory like any other Python.
