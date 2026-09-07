# Local Demo Runbook

This runbook starts the first repeatable MovieLens portfolio demo from a clean
checkout. It seeds the full 62,423-title MovieLens catalog from a committed
snapshot, of which 120 reviewed titles carry posters and synopses; downloading
or ingesting the 25M dataset is not required for the walkthrough. The full dataset
remains the source for training and offline evaluation.

## Prerequisites

- Docker Desktop or another Docker Engine with Compose v2.
- Enough free disk space for the Python, Next.js, Postgres, Keycloak, and
  pgBouncer images. The first start downloads base images and is substantially
  slower than later cached starts.
- Ports 3001, 5432, 6379, 6432, 8000, and 8080 available on localhost, plus
  9090 if you run the nightly load profile (it starts Prometheus as the k6
  remote-write receiver; the 60-second smoke does not). The demo does not start
  MLflow or Grafana.

Python and Node.js are not required on the host for the containerized
walkthrough. They are only required for direct backend or frontend development.

## First start

From the repository root:

```bash
cp .env.example .env
make demo-up
make demo-seed
make demo-smoke
```

`make up-dev` is the same thing under the name the multi-environment plan uses:
this stack — `docker-compose.yml` for the stores and Keycloak, plus
`docker-compose.demo.yml` for the application layer at `ENVIRONMENT=dev` over
the demo fixture's 515 interactions over the full MovieLens catalog — **is** the
dev environment. There is
deliberately no `docker-compose.dev.yml`; the only job a third file would have
is turning `DEV_AUTH_BYPASS` on, and this stack sets it to `"false"` precisely
so the browser journeys and the load gate authenticate against real Keycloak
tokens. `tests/unit/test_prod_compose.py` holds that decision in place, and
[`deployment-runbook.md`](deployment-runbook.md)'s "Staging" section covers the
other two environments.

`TMDB_READ_ACCESS_TOKEN` in `.env` is optional. Leave it empty to use the
generated poster artwork, or set a TMDB API Read Access Token before
`make demo-up` to enable real posters.

`make demo-up` does the following:

1. Builds the FastAPI and standalone Next.js images.
2. Starts isolated Postgres and Keycloak databases, Keycloak, and pgBouncer.
3. Runs a one-shot schema job that creates the ingest-owned base tables and
   applies every Alembic migration.
4. Starts FastAPI only after schema setup succeeds and pgBouncer is healthy.
5. Starts Next.js only after FastAPI is healthy.
6. Restarts the feature/model sidecars when both halves of the learned path
   survived: a serving bundle in the artifact volume, and rows in the Redis
   online store. When either is missing it says so in one line and leaves them
   for `make demo-seed` — the model sidecar refuses to boot against an empty
   online store rather than rank every candidate from missing features, so
   starting it there would only produce a timeout.
7. Verifies FastAPI, Next.js, and the Keycloak demo realm from inside the demo
   network.

`make demo-seed` can be run repeatedly. It preserves an existing full-ingest
catalog, inserts only missing catalog rows — the reviewed 120 first, then the
62,423-title MovieLens snapshot under them — refreshes the reviewed local
metadata snapshot, and replaces the controlled demo persona/background
interactions with the same deterministic fixture. It
then materializes the tenant's Feast features, trains the deterministic
item-item and LightGBM demo artifacts, and starts the private feature/model
sidecars after their registry and artifact volumes are ready. Re-running the
command recreates both sidecars so their in-process registry, model bundle, and
version-scoped feature cache cannot retain the previous snapshot.

`make demo-smoke` fails unless all of these contracts hold:

- FastAPI, Next.js, and the Keycloak demo realm are reachable.
- Action Fan, Drama Fan, Eclectic Viewer, and Cold Start are discoverable.
- Action Fan has history and recommendations with no seen-item overlap.
- Action Fan reports `item-item-cosine+lightgbm` with versioned artifacts.
- Cold Start has no history and reports the `popularity` fallback.

## Walkthrough

Open <http://localhost:3001>.

1. **Sign in.** The front door is the sign-in surface and nothing else — the demo
   runs with the dev bypass disabled, so **Continue with Keycloak** starts the
   real authorization-code + PKCE flow. Point out that the tokens stay in an
   encrypted HttpOnly server session and never enter browser storage, and that
   the signed-in actor is named separately from the MovieLens persona being
   explored. After sign-in, `/` redirects to `/discover?userId=900000101`: the
   product is the first thing a signed-in viewer meets, not a dashboard.
2. **Discover, as Action Fan.** The featured movie leads and the ranked rail
   follows. The policy label reads `Ranked by the learned model` for a warm
   persona and `Popular while we learn` for a cold one — it follows the
   `serving_policy` the response reported and is never inferred when the response
   states it. Switch persona in the address (`?userId=900000104`) to show Cold
   Start on the fallback label, with the cold-start routing rule stated as
   policy rather than guessed at. Recommendations and watch history load as
   independent regions, so a dead history query cannot take the movie decision
   with it. The featured slot is a queue position: `Watched`, `Watchlist`, and
   `Not for me` each advance it. When the featured title is already on the
   watchlist it carries an `On your watchlist` cue and a **Skip**, which advances
   the queue and writes nothing at all — not a dismissal, and never a training
   signal. After the third such skip in a session the page makes one inline
   offer, *Stop featuring titles on your watchlist?*; the same toggle lives
   permanently under **Featured picks**. Turning it off is durable per-persona
   state (`user_preferences`, forced RLS, migration 0014) rather than a
   browser setting, and it changes only the featured slot — the ranked rail still
   lists watchlisted titles, marked `In watchlist`, because the rail is the
   model's ranking and hiding items there would misrepresent it.
3. **`Why this?`** Open the drawer beside the featured movie: the API's own item
   reason, the serving policy, the model version, the tenant, and the correlation
   ID — every row built only from a field the response actually carried, and a
   missing field drops its row rather than blanking the panel. Then **Show
   prediction audit** for the durable audit row and the online feature values.
   That is two deliberate actions, neither on the server render, so the evidence
   can never delay the first movie. The ranker score appears only inside this
   disclosure, labelled as an uncalibrated ordering on the scale the response
   named — never as a match percentage.
4. **Browse, and back again.** Move to **Browse**. Type into `Search titles`, add
   a genre and a decade, change the sort: each of those is written into the URL,
   and each edit drops the cursor, because the endpoint binds a cursor to the
   query that produced it. **Load more movies** appends the next page
   de-duplicated, with no invented total. Open a movie, then use **Back to
   Browse** — the accumulated window and the scroll position come back instead of
   restarting at the top.
5. **The movie page, and movie state.** The detail page opens on a backdrop-led
   hero: tagline, year, runtime and genres, the TMDB score with its attribution,
   directors and a scrollable cast row, the overview, and a trailer that loads
   nothing from YouTube until the poster frame is pressed — worth demonstrating,
   because the privacy-enhanced embed is asserted to issue no request to any
   YouTube host before that press. All of it comes from an offline enrichment
   pass into a `details` JSONB column (migration 0013) returned by the detail
   endpoint alone, so no page fans out to TMDB per card, and a title without
   details degrades to the plain layout. Then use `Watchlist`, `Mark watched`,
   and the star rating, and point out what each one claims. Watchlist is
   organizational and seeds no candidates. Watched is one positive interaction.
   Rating commits before it celebrates — the staggered fill, pop and collapse
   into `You rated 4/5 · Change rating` wait for the API, so a write that rolls
   back is never celebrated, and the whole sequence is skipped under reduced
   motion.
   The panel says in as many words that star magnitude is display feedback today
   rather than a graded training signal. Removing watched history is the only
   destructive action, so it sits behind `Confirm removing <title> from watched
   history` and states the consequence before it can be committed. `Not for me`
   is a reversible exclusion — `Undo not for me` puts it back — and never becomes
   a negative training label.
6. **Library.** The **Rated**, **Watchlist**, and **Seen** tabs each load
   independently, so one failing tab leaves the others and the ratings summary
   readable. (`Seen` is the visible label only — `/library?tab=history` is still
   the address and `history` is still the API value.) Seen carries its own
   search, genre and release-year filters and five rankings, with cursors bound
   to the query fingerprint and an exact `matched` count rather than an invented
   total, plus a spotlight above the list that walks the same filtered rows one
   title at a time for a quick re-rate. Find the title just rated.
   `Remove rating` is quiet, unconfirmed, and leaves the movie in Seen;
   `Remove from history` is styled apart and confirmed, because it destroys a
   signal rather than adding one. That
   distinction is the one worth dwelling on — the two used to look like the same
   button. The taste summary is labelled `live-ratings-v1` and presented as a
   live read of current ratings, not as a model explanation.
7. **Quick Picks.** Reach it from Discover's **Rate a few in Quick picks** link;
   it is deliberately not a fourth navigation slot. One movie at a time, with
   `Not for me` (J), `Watchlist` (K), `Watched` (L), and `Undo` (U) available
   identically as buttons, keys, and swipes. The card advances only after the API
   commits, so a failure keeps the card and returns focus to the control that
   failed. The progress panel reads `<n> of 10 positive watched signals` and says
   how many more are needed before learned serving can be used — it reports the
   count the response carried and never announces a policy transition it has not
   observed.
8. **`/legacy`.** Follow the **Legacy dashboard** link in the shell footer. This
   is the pre-redesign Phase 3 surface, kept as the documented rollback for the
   cutover and labelled as such on screen. Its serving-contract panel reports the
   `Serving policy`, `Learned ranking`, and model version the response actually
   carried — or `Not read yet` — rather than the static `Popularity baseline`
   claim that used to contradict the deployed router. It is also the only place
   the four personas are offered as named chips; the product selects a persona by
   URL.
9. **Posters, and putting a persona back.** If a TMDB token is configured, point
   out the real posters and release years; otherwise show that the deterministic
   fallback artwork keeps the same flow usable. The product exposes no reset
   control, so to return a persona to cold start use `Clear ratings` on
   `/legacy`, or call `DELETE /users/{id}/ratings` directly — it drops every
   durable movie-state row for that persona. `make demo-seed` restores all four
   personas to the seeded fixture and is safe to re-run.

## Audit and latency proof

After generating at least one recommendation, print its three newest durable
audits:

```bash
make demo-audits
```

The response is read through the same authenticated, RLS-bound application
connection used by the rest of the API. For a warm result, point out the
request ID, exact ranked movie IDs and scores, eight online feature values per
prediction, candidate/ranker/feature versions, and the separate candidate,
feature, ranker, model, and total latency fields. Cold Start records
`popularity`, `fallback_reason: cold-start`, and no fabricated ranker features.

That is the *prediction* audit, and it covers one route. Every other
authenticated request writes an operational row into `request_audits` on the
same transaction — tenant, actor, persona, the matched route template, method,
status, outcome, latency and the same correlation id — readable at
`GET /users/{user_id}/request-audits`. It is the table to open when the
question is "what did this persona's session actually call", and the
correlation id is what joins a row there to the prediction audit for the same
click. Neither table stores a request body or a query string.

Run the authenticated smoke gate:

```bash
make demo-load-quiesce
make demo-load-smoke
```

`make demo-load-quiesce` stops the services the gate does not measure — the
browser demo's `api` and `web` plus the one-shot setup containers — so they
stop competing for CPU with the ones it does. It stops rather than removes
them, so `make demo-logs` still explains a failure afterwards, and
`make demo-up` brings them back. Skipping it is fine on a roomy laptop and is
the difference between measuring the service and measuring the host on a
shared CI runner.

`make demo-load-smoke` starts an internal `api-load` process with development
impersonation disabled and recreates the feature/model/load processes so each
run begins at the same process-cache boundary. It obtains a real Keycloak
token, then primes every uvicorn worker for every persona with real
authenticated requests — the sidecar's feature cache is keyed by user *and*
candidate set, so this is the only warm-up that pays for itself — and refuses
to start measuring if the stack is not serving the seeded personas. It then
targets 55 recommendation arrivals/second for 60 seconds with 10 preallocated
VUs and a ceiling of 40. The measured traffic follows a deterministic 7/2/3
warm/cold/mixed ratio. The command fails unless all recommendation checks pass,
request errors are zero, p99 is below 100 ms, and achieved throughput is above
50 requests/second. Warm personas must additionally report
`serving_policy.learned`: a request that quietly degrades to the popularity
fallback answers HTTP 200, and a latency gate that accepts it is timing the
wrong answer.

The final JSON object is the compact evidence summary. It includes p50, p95,
p99, achieved throughput, request count, total test-run duration, error/check
rates, warm-up cost, dropped iterations, and silent learned fallbacks. Dropped
iterations are kept visible as a capacity signal; the gate is based on achieved
throughput together with latency, correctness, and error thresholds. Note that
k6 divides the request count by the *whole* test-run duration, warm-up
included, so the reported warm-up cost is spent out of the achieved-rate
margin. The accepted 2026-08-20 implementation baseline reported p50 6.31 ms,
p95 14.27 ms, p99 41.30 ms, 54.08 measured requests/second, zero request
errors, and zero dropped iterations across 3,301 measured requests.

The current 2026-08-26 regression-fix run, after the catalog and durable-state
work landed, reported p50 7.24 ms, p95 12.50 ms, p99 48.99 ms, 54.18 measured
requests/second, zero request errors, zero dropped iterations, and zero silent
learned fallbacks across 3,300 measured requests. The four model-server
processes intentionally run with one native LightGBM/BLAS thread each. Removing
those limits lets each process create a host-sized OpenMP team and invalidates
both clean-start readiness and the latency baseline through internal CPU
oversubscription.

That is the local shape. GitHub's runner did not accept the same code at face
value — two runs with the pins in place still breached, at p99 198.97 ms and
164.14 ms, with cold traffic breaching exactly like warm — which ADR 0010's
second 2026-08-26 note records as a shared-path tail, not a ranker one. The
breakdown therefore now prints the handler's own percentiles beside k6's, per
traffic class and per serving policy, with a per-second `srv_p99` column, an
`fdatasync` baseline for Postgres's volume, and the WAL/IO counter deltas across
the window.

That instrumentation found it. On the first failing run it captured, the tail
was outside the handler on every traffic class, steal was zero, and the window
spent 9,703 ms on 3,085 WAL syncs — 3.15 ms to commit one audit row, against
0.21 ms on a passing run of the same code. **The CI load job therefore runs
Postgres's data directory on tmpfs**, via `docker-compose.ci-load.yml`, which
that job alone layers on by setting `DEMO_COMPOSE_EXTRA`. Nothing about
durability changes — `synchronous_commit` stays on and the commit still lands
before the response — only the medium under CI's WAL. **A local run is not on
tmpfs**: on Docker Desktop the data directory is a normal volume unless you pass
the file yourself, so a laptop's numbers still include its own disk. To
reproduce CI's shape locally, put the same override in front of every demo
target:

```bash
export DEMO_COMPOSE_EXTRA="-f docker-compose.ci-load.yml"
make demo-reset            # the tmpfs starts empty, so it has to be seeded on it
make demo-load-quiesce && make demo-load-smoke
```

Unset it afterwards. A stack started this way loses its whole database the
moment the Postgres container stops — `make demo-down`, a restart, a laptop
reboot — which is exactly right for a CI job that reseeds every time and rarely
what you want locally. `make demo-load-quiesce` is safe: it stops `web`, `api`
and the setup containers, never `postgres`. ADR 0010's 2026-08-28 note has the
full evidence table and what the decision deliberately stops measuring.

Everything the run produced lands under `artifacts/load-smoke/`, which CI
uploads on pass and fail alike:

```
artifacts/load-smoke/
├── docker-stats-{before,after}.txt   # CPU/memory and the effective CFS weights
├── cpu-stat-{before,after}.txt       # cgroup throttling counters per service
├── postgres-storage.txt              # yes/no/unknown: was the data directory on tmpfs
└── window-1/
    ├── summary.json                  # the JSON printed above
    ├── per-second.txt / .json        # p50/p95/p99/max per second, with steal
    ├── host-cpu.jsonl                # /proc/stat deltas, one line per second
    ├── raw-metrics.json.gz           # every k6 sample, for re-deriving anything
    ├── decision.json                 # the measurement-validity verdict
    ├── server-side.json              # audit rows for the window + WAL/IO counters after
    ├── server-side-before.json       # the same counters before the window
    ├── disk-fsync.jsonl              # fdatasync latency on Postgres's volume
    ├── started-at.txt                # the window's own start, which bounds the export
    └── k6-stdout.txt, k6-exit, breakdown.txt
```

The server-side export is what tells a slow *request* apart from a slow
*handler*: the audit row's `latency_ms` is timed around the handler only, so the
difference between it and k6's number is auth, pooling, the audit insert, and
the commit's `fdatasync`. `disk-fsync.jsonl` holds a burst before the window by
default; `LOAD_FSYNC_PROBE=on` samples during it as well, which is diagnostic
only — an `fdatasync` flushes the device, and the probe measurably slows the
service it is watching.

The breakdown closes with a **storage** block, which is where to look when the
handler is fast and the request is not:

```
[load-summary] storage under the measured window:
  Postgres data directory: tmpfs — the host's block device is out of this measurement
  commit cost: 0.214 ms per WAL sync across 3,385 syncs (baseline 0.50 ms, flagged above 2.00 ms)
  storage_stall: no — informational: WAL sync time per sync > 4x the 0.50 ms baseline ...
```

The first line says what the window actually measured, read off the running
container rather than assumed from the job — `tmpfs` in CI, `not tmpfs` on a
laptop, `not recorded` on an evidence directory captured before this existed.
The second is what one commit cost, from Postgres's own WAL counters.
`storage_stall: yes` means that cost is more than four times the 0.5 ms baseline
over at least 100 syncs, which on past runs has meant the device rather than the
service — but it is **informational and changes nothing**: the gate's verdict is
k6's, and the only thing that can buy a re-measurement is the CPU-steal rule
below.

The per-second table is the thing to read first when the gate fails. A slow
opening second is a cold cache; a tail smeared across the middle is contention;
a tail that follows one traffic class is a serving regression. Each second also
carries the host's CPU **steal** — time the hypervisor spent elsewhere while
this kernel had work to run — and its run-queue depth. The ten slowest seconds
are printed into the log so a failure is readable without downloading anything.

**The re-measure rule.** A breached window is re-measured exactly once, and only
when at least three of its ten slowest seconds recorded 10% or more CPU steal:
that combination means the machine was not scheduled, which is a measurement to
redo rather than a result to report. The repeat reuses the warm stack, its
verdict is final however its own steal looks, and the decision is labelled in
the log ("re-measured after hypervisor steal: N%"). A breach with low steal is
never re-measured — it is the service's and fails immediately. This is a
validity rule about when a number counts, not a threshold: no threshold,
arrival rate, run length, or traffic mix changes.

Both local and CI runs use the exact k6 image version pinned in
`infra/ci/k6-version` and remote-write their measurements to the local
Prometheus receiver. The 60-second smoke holds its remote-write batch until the
scenario has ended, then k6's final flush publishes the samples. This prevents
the load generator's own five-second exporter batches from contaminating the
p99 it is measuring. The larger profile is available separately:

```bash
make demo-load-nightly
```

That profile targets 600 requests/second for five minutes with 100 VUs. Treat
it as a capacity probe: a laptop may fail to generate or serve that target. The
staging Compose environment now exists (`docker-compose.staging.yml`), but a
*scheduled* staging run still does not: there is no staging host to schedule it
against, and running it on the laptop that also hosts the stack measures the
laptop. It stays deferred on a host rather than on the Compose file.

The API container deliberately enables the guarded development impersonation
mode for tenant `demo`; the browser therefore needs no manual token during this
portfolio walkthrough. `Settings` refuses to start with that bypass in any
non-development environment.

### Page-shaped budgets and browser timing

The gate above measures one endpoint. Two further commands measure what a
*page* costs, and they are kept separate on purpose — the frontend testing
strategy forbids conflating browser timing with the serving-only k6 number, so
they run in different suites and produce different reports.

```bash
make demo-load-pages          # page-shaped API workloads, per step
make demo-reliability-check   # request ids, readiness, degraded metadata, ...
```

`make demo-load-pages` runs `synthetic/load/pages.js` through the same wrapper
as the smoke gate — same warm-up, same host-CPU probe, same re-measure rule —
and models five routes as tagged step sequences read off the web client's own
loaders:

| Scenario | What it drives |
|---|---|
| `discover` | recommendations + history + personas concurrently, then the audits/features disclosure |
| `browse` | catalog first page → next cursor → next cursor → open a movie, across the search/genre/decade/sort variants |
| `library` | the active tab + taste profile + personas, then the two tab switches |
| `mutation` | read state → mutate → replay the idempotency key → read state → counts refresh → list read → revert → read state |
| `quickpicks` | recommend → dismiss → recommend → undo → watched → recommend → revert |

The two writing scenarios follow the same persona ownership table the browser
journeys use (`web/tests/e2e/browser-auth.spec.ts`): rating and watched history
on Action Fan, watchlist on Eclectic Viewer, Discover's writes on Drama Fan.
They put every change back inside the iteration, and `teardown()` sweeps
anything left and fails the run if it had to. Cold Start `900000104` is never
mutated — the script refuses to start if a writer is pointed at it, and
teardown reads its policy back to prove nothing moved its signal count. Run the
browser suite and this target one at a time locally; in CI they use different
Compose projects against different databases and cannot collide.

Correctness always fails the run: every check, zero request errors, zero
unreverted mutations. The per-step latency budgets in
`synthetic/load/page_thresholds.js` are **advisory** by default and reported
rather than enforced (`PAGE_LATENCY_ENFORCED=true` enforces them, and
`make demo-load-pages-nightly` does over a three-minute window). ADR 0010's
2026-08-21 page-shaped note carries the budgets, the baselines they came from,
and what it takes to promote them.

The evidence lands under `artifacts/load-pages/` in the same shape as the smoke
gate's, plus `window-1/steps.txt` — the per-step table with each step's
percentiles next to its budget, which is the first thing to read when a budget
slips. `reliability.json` sits alongside it.

`make demo-reliability-check` reports ten pass/fail facts a percentile cannot
express: `/healthz` reachable without a token while nine other routes answer
401; a caller-supplied `X-Request-ID` echoed on the response *and* persisted to
the audit row's `correlation_id`; auth, model and database provenance readable
from `/whoami` and the audit row; bounded page sizes; a cursor reused under a
different filter refused with 400; and a poster-less movie rendering as a record
rather than a failure. Rate limiting is one of the required checks: it asserts
`X-RateLimit-*` on an admitted response and, once the bucket is drained, a `429`
carrying `Retry-After`, `X-RateLimit-Remaining: 0` and a JSON `detail` — with no
third outcome allowed. **On this stack it will report that the target has no
limiter, and that is correct**: ADR 0014 turns the token bucket off in `dev`
precisely so the load harnesses can drive a single Keycloak identity far past
any sane per-subject rate. The check records that absence in words rather than
passing over it silently, and names the fact that every deployed environment
should instead show the enforced branch. To see the enforced branch locally, run
it against the production-mode stack (`make up-prod`), where `ENVIRONMENT` is
`production` and the limiter is on by default.

Browser timing is a Playwright suite, not a load test:

```bash
cd web && npm run test:perf     # needs the demo stack up and seeded
```

It signs in through real Keycloak, warms each route, then measures LCP, CLS, and
time-to-visible-acknowledgement on the agreed mobile profile — 390x844, device
scale factor 3, touch, 4x CPU throttle, no network throttling — and asserts the
structural promises: reserved poster boxes, below-fold lazy loading, a bounded
catalog page, zero per-card TMDB requests, and technical evidence that loads on
disclosure rather than blocking the first movie. Targets are LCP ≤ 2.5 s,
CLS ≤ 0.1, and acknowledgement ≤ 100 ms; CLS, LCP and the structural claims are
enforced, the acknowledgement budget is advisory for now. Each route is measured
against the persona whose journey already owns that kind of write, and Cold
Start is read only — Quick Picks owns it for its signal counter, and timing an
animation is not a reason to spend the signal it is counting. A route that is
missing is skipped and listed as skipped, whether it answers 404 or redirects
somewhere else; the report names which. It is written to
`artifacts/browser-timing/browser-timing.json` with a compact table in the log.

## Routine operations

```bash
make up-dev      # the dev environment's name for `make demo-up`
make demo-logs   # tail the services that explain startup/runtime failures
make demo-down   # stop every container, including the load profile, and preserve demo volumes
make demo-up     # restart while preserving the database and the online store (also undoes a quiesce)
make demo-reset  # delete only movielens-demo volumes, rebuild, migrate, and reseed
```

`make demo-down` and `make demo-reset` enable the `load` profile, so they also
cover the `api-load` and `k6` containers a load run starts. Without that,
`api-load` survived a `down` and held the network open behind it.

The volumes `make demo-down` preserves are Postgres, Keycloak's Postgres, the
Feast registry, the model artifacts, and the Redis online store — so
`make demo-down && make demo-up` comes back serving the learned path with no
reseeding. Redis is a named volume for exactly that reason: it is the online
feature store rather than a cache, and losing it is losing the features every
ranking score is computed from.

The Compose project name is pinned to `movielens-demo`. `make demo-reset` cannot
remove volumes belonging to the normal development Compose project, but it does
permanently delete the isolated demo Postgres and Keycloak data.

## Troubleshooting

- **A port is already allocated:** stop the normal development stack with
  `make infra-down`, then rerun `make demo-up`.
- **Schema setup failed:** run `make demo-logs` and inspect `demo-setup` and
  `postgres`. The API will not start after a failed setup job.
- **FastAPI is unhealthy:** inspect `api` and `pgbouncer` logs. Startup checks
  reject a BYPASSRLS application role or non-transaction pooling.
- **Personas are missing:** run `make demo-seed`, then `make demo-smoke`.
- **`make demo-up` says the online feature store is empty:** the serving bundle
  outlived the Redis volume — a removed volume, or a stack seeded before that
  volume was named. The sidecars are deliberately left down; `make demo-seed`
  materializes the features and starts them.
- **Warm personas show `popularity`:** inspect `model-server`, `feature-server`,
  and `api` with `make demo-logs`, then rerun `make demo-seed`. The API falls
  back deliberately when artifacts, online features, or the sidecar are invalid.
  If the audit reason is `model-server-unavailable: ReadTimeout`, confirm the
  effective model-server environment still sets `OMP_NUM_THREADS`,
  `OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS`, and `VECLIB_MAXIMUM_THREADS` to
  `1`; do not compensate by raising the 0.5-second sidecar timeout.
- **`make demo-audits` returns no rows:** generate a recommendation for Action
  Fan first. If the request succeeded but no row appears, inspect `api`; audit
  persistence is part of the request transaction and should fail the request
  rather than silently dropping a row.
- **The load gate fails:** read `artifacts/load-smoke/window-1/per-second.txt`
  first, then use the emitted JSON to separate response errors,
  bad policy/check results, throughput saturation, and a p99 regression. A
  non-zero `silent_learned_fallbacks` means warm personas were served by the
  popularity fallback — look for `model-server-unavailable` in `model-server`
  and `api-load`. A non-zero `dropped_iterations` means the load generator
  could not start every arrival, so the percentiles understate the tail; treat
  the run as capacity-limited rather than as evidence either way. A flat p50
  with a moved p99 is a contention signature, but the contention can still be
  inside the service: use the recorded CPU-steal decision before blaming the
  host, confirm `make demo-load-quiesce` ran, and verify the model-server native
  thread limits above. Then compare `srv_p99` with the k6 p99 in the slowest
  seconds: a fast handler under a slow request puts the time in auth, pooling,
  or the commit's `fdatasync` — read the storage block and the `fdatasync`
  baseline in the same breakdown before blaming the ranker, and note that
  `storage_stall: yes` on a run whose data directory reports `not tmpfs` is the
  laptop's disk rather than a regression — while a slow handler is the service.
  Then inspect
  `api-load`, `model-server`, `feature-server`, `pgbouncer`, and `postgres`
  with `make demo-logs`.
- **Posters are missing:** this is expected without a TMDB token. If a token is
  configured, restart with `make demo-down && make demo-up` so FastAPI reads the
  new environment.
- **A clean rebuild is required:** run `make demo-reset`. This is the recovery
  path for stale or incompatible demo volumes.
