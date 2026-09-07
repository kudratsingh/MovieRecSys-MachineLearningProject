# Production Deployment Runbook

This is the operational companion to [ADR 0013](adr/0013-production-deployment-target.md), which pins
the target — **one Hetzner Cloud CX22 running `docker-compose.prod.yml` behind its own Caddy edge** —
and the reasoning behind it. Everything below is the order to do things in and the traps that are
expensive to discover live.

Start with the one thing that surprises everybody: **there is no single `DATABASE_URL` for this
system.** Four Postgres identities are in play, they are not interchangeable, and using the wrong one
fails in a different way each time — silently, loudly, or at boot.

| Identity | Privilege | Created by | Used for |
|---|---|---|---|
| `postgres` (the `postgres-app` container's superuser) | SUPERUSER | the Postgres image, from `POSTGRES_SUPERUSER_PASSWORD` | The one-time provisioning SQL, and nothing else. It never reaches a serving container |
| `migrator` | LOGIN, **BYPASSRLS** | the provisioning SQL; migration `0001` leaves it alone | The release job only: `create_tables()` then `alembic upgrade head`, plus the persona seed. Owns every base table |
| `admin_user` | LOGIN, **BYPASSRLS** | the provisioning SQL; migration `0001` leaves it alone | `model-server`, `feature-server` and materialization — cross-tenant reads and writes, direct to Postgres, bypassing the pooler by design |
| `app_user` | LOGIN, plain — RLS applies | the provisioning SQL; migration `0001` leaves it alone | **The only role that serves a request.** Reaches Postgres through pgBouncer's `movielens_app` forced-user alias. The API refuses to boot if this role has BYPASSRLS or SUPERUSER |

A fifth name, `pgbouncer_admin`, is a **pgBouncer-internal identity with no Postgres role behind it**
— the provisioning SQL deliberately does not create it. It exists only in the pooler's own userlist
because the API's boot check opens the pooler's admin console with it. A sixth, `pgbouncer_auth`, *is*
a real Postgres role and is the one the pooler uses to look up SCRAM verifiers.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="diagrams/postgres-identities.dark.svg">
  <img alt="Which Compose service connects as which Postgres identity: the api through pgBouncer's movielens_app alias as app_user, the sidecars and materialization direct as admin_user, release and backup as migrator, provisioning as the superuser, plus the two pgBouncer-internal names." src="diagrams/postgres-identities.svg" width="100%">
</picture>

## What is deployed

One Compose project (`movielens-prod`) on one machine. Nine long-lived services — `postgres-app`,
`postgres-keycloak`, `redis`, `pgbouncer`, `keycloak`, `api`, `model-server`, `feature-server`, `web`
— plus the `edge`, and the run-to-completion jobs behind the `jobs` profile: `postgres-provision`,
`keycloak-provision`, `release`, `materialize`, `verify`, `canary`, `loadcheck`, `backup` and
`rollback-rehearsal`.

**Two hostnames, and only two:** `https://<PUBLIC_APP_HOST>` → `web`, `https://<PUBLIC_AUTH_HOST>` →
`keycloak`.

> **Hard rule.** The only ports bound on the host are **80 and 443** (the edge) and **22** (SSH,
> key-only). `api`, `model-server`, `feature-server`, `pgbouncer`, `redis` and both Postgres services
> publish nothing and are reachable only from inside the Compose network. `feature-server` has no
> authentication of any kind and the model sidecar's `/healthz` is unauthenticated; both are safe
> only because nothing outside the host can reach them. If you ever add a `ports:` entry to debug
> something, remove it in the same session — a temporary port is how this becomes an incident.

The images come from GHCR and the box never builds. `docker-compose.prod.yml` keeps its `build:`
contexts so the same file builds locally for the rehearsal (`make up-prod`), while on the host
`IMAGE_REPOSITORY` and `IMAGE_TAG` resolve every service to a published image.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="diagrams/production-topology.dark.svg">
  <img alt="The production topology: one Hetzner CX22 with only the Caddy edge publishing ports and key-only SSH from GitHub Actions, ten long-lived services on a private network, the jobs profile, and the systemd units for boot, nightly backup and weekly prune." src="diagrams/production-topology.svg" width="100%">
</picture>

## Staging

Numbered sections start below; this one is context, because the first question anyone asks about a
runbook like this is whether there is somewhere to try it first. There is.

**Staging is `docker-compose.prod.yml` with `docker-compose.staging.yml` layered on it** — a
different Compose project (`movielens-staging`), `ENVIRONMENT=staging` on the eight services
production gives an environment label (the seven that construct `Settings()`, plus the Feast
server), and its own `.env.staging`. Nothing else differs, deliberately: hostnames,
every credential, the certificate issuer and the image tag all travel in the env file, exactly the
way the box and the laptop rehearsal already differ from each other.

```bash
cp infra/deploy/staging.env.example .env.staging   # then fill in every REPLACE_ME__
chmod 0600 .env.staging
make up-staging        # build the images from this checkout, then start the stores
make staging-release   # roles, realms, migrations, the persona seed, materialization
make staging-serve     # the serving tier, held until the sidecar is warm rather than listening
make staging-verify    # the post-deploy matrix, then the reliability suite
make staging-reset     # throw it away; the next rehearsal starts from empty volumes
```

`make staging-pull IMAGE_TAG=<40-character sha>` replaces the build step when the images should come
from GHCR — which is the mode worth using before a release, because the artifact staging rehearses is
then the artifact the box will run.

**What staging is for:** rehearsing a release end to end before a merge to `main` deploys it. The
release order, the release jobs, the migrations, the Keycloak provisioning and the whole verify
matrix are the production ones. When a release is going to fail, this is where it should.

**What staging deliberately is not:**

- **Not a second production.** There is no staging deploy workflow, no staging canary, no scheduled
  staging backup and no staging entry in the deploy gate. `deploy.sh` is production's, and there is
  no `staging-deploy` target — exercising the deployment's operational path against the environment
  that is allowed to be broken teaches the wrong thing about both.
- **Not a relaxed environment.** Every guard in `src/config.py` is written `environment != "dev"`, so
  `staging` refuses the auth bypass, refuses the checked-in model-server token, refuses the
  checked-in pgBouncer password and runs the ADR 0014 rate limiter — all of it exactly as production
  does. A staging build that relaxed a production guard would rehearse a system nobody deploys.
- **Not a host that exists.** Nothing is deployed to staging either; today it runs on a laptop, from
  this checkout, with `EDGE_TLS=internal` and `staging.app.localtest.me` / `staging.auth.localtest.me`
  hostnames that resolve to 127.0.0.1 with no DNS. Pointing it at a second box is two hostnames and
  `EDGE_TLS=acme` in `.env.staging` and nothing else — but until somebody does that, this is a local
  environment and the documents should not imply otherwise.

Two practical notes:

- **Staging and the production rehearsal cannot run at the same time on one machine.** Both bind 80
  and 443, and those ports are not really adjustable — the public origins carry no port, and
  Keycloak's issuer and Auth.js's callback URL are built from them. `make prod-down` before
  `make up-staging`, and the reverse. The volumes are separate, so neither loses state to the other.
- **Trust the edge before you use a browser.** Staging defaults to Caddy's own CA, so
  `make staging-edge-ca > /tmp/staging-root.crt` and add that root, or expect a certificate warning
  on every page. The containers already read the same root from the shared `edge_ca` volume.

The dev environment is the other side of this: `make up-dev` starts `docker-compose.yml` +
`docker-compose.demo.yml` — the stores, a Keycloak with dev credentials, and the application layer at
`ENVIRONMENT=dev` over the demo persona fixture. It is the only environment where the auth
bypass is permitted at all, and it is documented in [`demo-runbook.md`](demo-runbook.md). There is
deliberately no `docker-compose.dev.yml`; `tests/unit/test_prod_compose.py` records why.

## 0. Decisions to record before the first deploy

Each row has a default. Replace `☐` with `☑` and a date when you accept it, or write the override in
place. Deploying with a row still open is how a decision gets made by accident.

The numbering skips D3 and D6, and that is deliberate. An earlier draft of this table was written
against a managed-platform hosting shape that was costed and rejected (ADR 0013); its plan-tier row
(D3) has no equivalent on a machine you rent outright, and its "how is a rollback performed" row (D6)
is answered outright by `infra/deploy/deploy.sh --rollback`. The remaining numbers are left as they
were so that older notes still resolve.

| # | Decision | Default | Recorded |
|---|---|---|---|
| D1 | Do the native-parallelism pins (`OMP/OPENBLAS/MKL/VECLIB_MAXIMUM_THREADS=1`) ship on the serving image? | **Yes — already baked into `infra/features/Dockerfile`.** They are the difference between p99 ≈49 ms and p99 ≈904 ms on the same hardware | ☑ 2026-08-27 — in the image, not a variable |
| D2 | The domain | Own domain, `app.` and `auth.` subdomains, Let's Encrypt via the edge. The issuer is baked into every token and both clients' redirect URIs, so changing it later is a re-provisioning, not an edit | ☐ |
| D4 | Keycloak's admin console is internet-reachable at `https://<PUBLIC_AUTH_HOST>/admin`, and the edge routes hostnames rather than path policies | Accept, with: a generated 48-character realm-admin password that lives only in `.env.prod` and the password manager, a separate named human admin (`KC_HUMAN_ADMIN_*`) as the account you actually sign in with, and brute-force protection on both realms. Note the bootstrap admin **cannot** simply be deleted — see §7. Otherwise add a Caddy matcher restricting `/admin` to a known address | ☐ |
| D5 | Persona impersonation: any signed-in `demo`-realm account can read **and mutate** all four personas | `registrationAllowed: false` (already seeded) plus exactly three deliberately-created accounts, and a line on the sign-in door saying the published account drives shared named personas | ☐ |
| D7 | Postgres TLS | Keep all Postgres traffic on the host's private Docker network; `server_tls_sslmode = prefer` in pgBouncer gives the `app_user` leg TLS for free. Recorded in ADR 0013 — requiring TLS everywhere is a DSN-construction change plus a Feast config change | ☐ |
| D8 | Retention for `feature_store.*`, which gains one generation per release and is never pruned | Delete-then-insert per `as_of`, keeping the last three generations — bounded by construction rather than by attention. On a 40 GB disk this is a leak, not a bill | ☐ |
| D9 | Do the browser journeys run against production? | No. One **read-only** canary spec at most (sign in, load `/` and `/discover`, assert the learned policy label, sign out). The mutating set writes real rows and stays on the seeded Compose stack in CI | ☐ |
| D10 | Is one serving tenant enough for the MVP? | Yes: `demo` serves the product, `default` exists so the isolation canary has a subject that must be denied. A second serving tenant needs a second sidecar process on a 4 GB machine | ☐ |
| D11 | Does a boot-time schema for the `web` environment variables land before or after the first deploy? | After. Every web variable has a silent `?? "http://localhost:…"` fallback, and a missing `AUTH_SECRET` fails **only on mutations** while reads keep working. Not a boot blocker; land it as a small web PR in the first post-deploy week | ☐ |
| D12 | Who holds the generated secrets, who owns rotation, and which GitHub environment gates the deploy? | A password manager as the authority; `/opt/movielens/.env.prod` (0600) as the only runtime copy; a `production` GitHub environment holding **only** `DEPLOY_SSH_KEY` and `DEPLOY_KNOWN_HOSTS` | ☐ |
| D13 | Where do the off-box backups go? | A free-tier object store at a **different** provider — Backblaze B2 (10 GB free) or Cloudflare R2 (10 GB free). Both dumps together are megabytes | ☐ |

## 1. Create the machine

1. In the Hetzner Cloud console, create a project and add an **SSH key** (your own, for the first
   login as root). Create a **CX22** — 2 vCPU x86, 4 GB RAM, 40 GB disk — with **Ubuntu 24.04**, in
   whichever location is nearest your audience, with IPv4 enabled. **Do not pick a CAX (ARM) type:**
   every image this project publishes is `linux/amd64` and nothing has been proven on arm64.
2. Attach a **Cloud Firewall** with inbound rules for **22, 80 and 443 only** (TCP, any source), and
   leave outbound unrestricted — the box needs GHCR, Let's Encrypt, the backup remote and Ubuntu's
   archives. The firewall filters before packets reach the machine; `ufw` on the box is the second
   layer, and the two are documented as the same three ports on purpose.
3. Optional but recommended: enable Hetzner's **backups** for the server (a percentage surcharge) as
   a whole-machine snapshot. It is not a substitute for §10 — a snapshot at the same provider is one
   account away from the same total loss as the data — but it makes "I broke the OS" a ten-minute
   recovery instead of a rebuild.

Record the IPv4 address. Everything below refers to it as `<ip>`.

## 2. DNS

Create two **A records** pointing at `<ip>`:

```
app.<domain>.   A   <ip>
auth.<domain>.  A   <ip>
```

Keep the TTL modest (300–600 s) until the deployment has settled: the issuer in every token is
derived from `auth.<domain>`, so a move later is a DNS change plus a re-provisioning, and a day-long
cached record makes that much worse.

**Both records must resolve before the first deploy.** Caddy asks Let's Encrypt for certificates on
exactly these names and an HTTP-01 challenge it cannot answer fails issuance — it does not fall back
to anything. Verify from somewhere that is not the box:

```bash
dig +short app.<domain> auth.<domain>
```

## 3. Bootstrap the host

```bash
ssh root@<ip>
apt-get update && apt-get install --yes git
git clone https://github.com/kudratsingh/MovieLens-RecSys.git /opt/movielens
/opt/movielens/infra/host/bootstrap.sh "$(cat deploy-key.pub)"
```

The argument is the **public** half of the key CI will deploy with; generate the pair on your own
machine (`ssh-keygen -t ed25519 -C movielens-deploy -f deploy-key`) and keep the private half for
§6. Passing `-` instead reads the key from stdin, which keeps it out of the shell history of a box
you do not fully trust yet.

What the script does, all of it idempotent: creates the unprivileged `deploy` user and installs that
authorized key; turns **password authentication off** in sshd; sets `ufw` to allow only 22/80/443;
enables unattended security upgrades; installs Docker CE and the Compose plugin from Docker's own apt
repository; writes `/etc/docker/daemon.json` from `infra/host/docker-daemon.json` so every container's
`json-file` log is capped at **20 MB × 5**; and installs and enables three systemd units —
`movielens.service` (brings the stack up at boot), `movielens-backup.timer` (nightly 04:00 UTC) and
`movielens-prune.timer` (weekly image prune older than seven days, **never** volumes).

Re-run it after any edit to it. A second run should change nothing and say so; that property is the
only thing keeping the script an accurate record of the machine.

Then check the three things worth checking before going further:

```bash
ufw status                         # 22, 80, 443. Nothing else
docker compose version             # the plugin, not docker-compose
systemctl is-enabled movielens.service movielens-backup.timer movielens-prune.timer
```

`bootstrap.sh` deliberately does **not** put secrets on the box, deploy anything, or configure DNS.

## 4. Generate `.env.prod`

Everything the stack needs is one file, owned by `deploy`, mode 0600, never in git:

```bash
ssh deploy@<ip>
cd /opt/movielens
cp infra/deploy/production.env.example .env.prod
chmod 0600 .env.prod
```

**Generate every password, token and secret with this exact command:**

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

This is not a style preference. Every DSN in `src/config.py` is built by f-string concatenation with
no URL-encoding, so a password containing `@` silently mis-parses the host and one containing `%` is
percent-decoded. pgBouncer's `userlist.txt` is one `"user" "password"` pair per line, so a quote or a
space produces a file that parses as something other than what was meant — and presents as a wrong
password rather than as a malformed file. Do not use `openssl rand -base64`; it emits `+`, `/` and
`=`. Generate a **different** value for every `REPLACE_ME__`.

`infra/deploy/production.env.example` is the whole contract and carries a comment per variable, so
work through it top to bottom rather than from a list here. The values that are not secrets and that
this deployment must set:

- `PUBLIC_APP_HOST` / `PUBLIC_AUTH_HOST` — the two names from §2, as hostnames, not URLs. Every
  origin and issuer is derived from them, which is what stops a certificate and an origin describing
  different names.
- `EDGE_TLS=acme` and `ACME_EMAIL=<you>@<domain>` — production certificates. The local rehearsal is
  the only place `EDGE_TLS=internal` belongs.
- `IMAGE_REPOSITORY=ghcr.io/kudratsingh/movielens-recsys` and `IMAGE_TAG` — the tag is rewritten by
  `deploy.sh` on every release; the value in the file is just the fallback.
- Sizing for this machine: `API_WORKERS=2`, `MODEL_SERVER_WORKERS=2`, `JAVA_OPTS_KC_HEAP=-Xms128m
  -Xmx512m`. The memory limits themselves live in `docker-compose.prod.yml` and are tuned to what the
  rehearsal measured; changing a worker count without re-checking them is how a container starts
  getting OOM-killed.
- `RCLONE_CONFIG_REMOTE_*` and `BACKUP_AGE_RECIPIENT` — §10.

Two things that used to be dangerous here and are not on this deployment, worth knowing because they
are still true elsewhere. `KEYCLOAK_AUTHORIZED_PARTIES` is typed `tuple[str, ...]` and parsed as
JSON, so the comma-separated form crash-loops the container — but the value is a literal in
`docker-compose.prod.yml`, not something anyone types into a panel. And the three settings that must
agree exactly (`KC_HOSTNAME`, `KEYCLOAK_PUBLIC_BASE_URL`, `KEYCLOAK_PUBLIC_ISSUER`) are all derived
from `PUBLIC_AUTH_HOST` in the same file, so they cannot disagree unless someone edits the compose
file to make them.

Dev-only variables are **absent, not `false`**: `DEV_AUTH_BYPASS`, `DEV_BYPASS_TENANT`,
`DEV_BYPASS_USER`, `MOVIELENS_UI_FIXTURE_MODE`. `Settings` refuses to construct with the bypass set
outside `dev`, so the enforcement exists — but it is a crash-loop, and a variable that is not there
cannot be flipped by accident.

## 5. The one-time provisioning SQL

**There is exactly one copy of this SQL: `infra/deploy/provision-roles.sql`.** Do not retype it from
memory or from an older document — a second copy is how the `ALTER DEFAULT PRIVILEGES` line goes
missing, and its absence fails *every* deploy rather than the first one.

On this deployment you do not run it by hand: the `postgres-provision` job in
`docker-compose.prod.yml` mounts `infra/deploy/provision-roles.sql` and
`infra/postgres/pgbouncer-auth.sql` read-only and applies them, as the container's own superuser,
before anything else starts. `make prod-stores` runs it, `make prod-release` depends on that, and so
every deploy re-applies it. It is idempotent (`ALTER` where the role already exists), which is also
how a password rotation is applied — edit `.env.prod`, then:

```bash
cd /opt/movielens && make prod-stores
```

What it creates: `app_user` (plain LOGIN, deliberately **not** BYPASSRLS), `admin_user` and
`migrator` (both BYPASSRLS), and `pgbouncer_auth`, each with the generated password passed in as a
psql variable so no secret is written into a file or into the server log. Migration `0001`'s
`IF NOT EXISTS` guards then leave those three alone — which is what keeps the tree-literal passwords
`app_user` / `admin_user` / `migrator`, published in a public repository, from ever taking effect.

The one statement worth reading before you run it:

```sql
ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public
    GRANT SELECT ON TABLES TO admin_user;
```

The model-server's schema fence reads `public.alembic_version` to learn whether the release job's
schema has arrived, and it runs as `admin_user`. `alembic_version` is created by whoever ran the
migration and carries no `GRANT` of its own, so without this line the fence fails with
`insufficient_privilege` on every deploy, and `src/release/bootstrap.py` raises a `ReleaseError`
naming this exact statement. Default privileges apply only to objects created *after* they are set,
so it has to be in place before the first migration. It grants nothing meaningful — `admin_user` is
already BYPASSRLS.

Two more properties of the file, in case an older copy of it says otherwise: it never wraps role
creation in a `DO $$ … $$` block (psql does not interpolate `:'var'` inside dollar quotes — the roles
would be created with the literal text as their password and nobody could log in), and it does not
create `pgbouncer_admin` or transfer ownership of the database or schema `public`.

`infra/postgres/pgbouncer-auth.sql` runs second because it installs a `SECURITY DEFINER` lookup
function executable only by `pgbouncer_auth`, and raises a named exception rather than half-installing
if that role does not exist yet.

**`PGBOUNCER_AUTH_MODE=userlist`, and this is measured rather than preferred.** `auth_query` was the
mode this deployment wanted, and the rehearsal established that it cannot be used here: both
`[databases]` entries pin a forced user (`user=app_user`, `user=admin_user`), so pgBouncer opens the
server connection under its own identity rather than passing the client's SCRAM exchange through, and
in `auth_query` mode it has nothing to present. The client leg succeeds and only the second leg
fails, which is worth recognising in the log:

```
LOG  C-…: movielens_app/app_user@… login attempt: db=movielens_app user=app_user
LOG  S-…: movielens_app/app_user@…:5432 new connection to server
WARNING server login failed: FATAL password authentication failed for user "app_user"
```

`userlist` renders `/etc/pgbouncer/userlist.txt` at 0600 at container start — never into an image
layer, never into the repository. `pgbouncer_auth` and its lookup function stay provisioned
regardless: switching back is a variable change with no rebuild the moment the forced users go. Note
that pgBouncer's healthcheck (`pg_isready`) does **not** authenticate, so a pooler in a mode that
refuses every login still reports healthy; the failure surfaces at the first service that connects.

## 6. GitHub environments, secrets, and the image registry

Two environments, holding the same values and differing only in protection rules:

| Environment | Protection | Secrets | Variables |
|---|---|---|---|
| `production` | required reviewer (your choice) | `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS` | `DEPLOY_HOST` = `<ip>`, `DEPLOY_USER` = `deploy` |
| `production-canary` | **none — deliberately no protection rules** | the same two secrets | the same two variables |

The second is not redundant: `.github/workflows/production-canary.yml` runs on a schedule, and a
required reviewer on a job that fires every thirty minutes means every canary queues for approval —
that is, no canary at all.

Until this section is done the schedule still fires: with **none** of the four values set the canary
ends as a green no-op with a notice saying so, and starts verifying the moment they exist. A
*partially* configured environment — a secret deleted, a variable mistyped — fails loudly instead,
so a canary can go quiet only by never having been armed, not by breaking.

- `DEPLOY_SSH_KEY` is the **private** half of the key from §3, PEM, newlines included. The workflow
  loads it into an `ssh-agent` for the job's lifetime and never writes it to disk.
- `DEPLOY_KNOWN_HOSTS` is the box's host key, captured **once** from a machine you trust:
  ```bash
  ssh-keyscan -t ed25519 <ip>
  ```
  Compare the fingerprint against the one Hetzner's console shows for the server before you paste it.
  The workflows pin this and never use `StrictHostKeyChecking=no`, so a substituted host key fails
  the deploy instead of being trusted silently.

**The images.** `.github/workflows/ci.yml`'s `publish-images` job runs only on a push to `main`, only
after every other job has succeeded, logs into GHCR with the workflow's own `GITHUB_TOKEN`
(`permissions: packages: write`), and pushes each image for `linux/amd64` under both the 40-character
commit SHA and a moving `main` tag. Two things to settle once, in GitHub's package settings:

- **Visibility.** Packages start private even for a public repository. Either make each package
  public — they contain no secrets, and the repository is already public — or create a read-only
  `read:packages` token and run `docker login ghcr.io` once as `deploy` on the box. Public is
  simpler and is what the deploy path assumes.
- **Retention.** Do not enable any policy that expires SHA tags. The SHA tag *is* the release
  identity, and a rollback past the box's seven-day local image window needs it to still exist.

## 7. The first deploy

Run it from the Actions tab: **Deploy production** → *Run workflow* → `sha` = the commit to deploy,
`rollback` = false. (After that, every merge to `main` deploys itself: the workflow triggers on CI's
successful run and re-asserts the SHA's CI conclusion before it opens an SSH connection.)

What happens on the box, all inside `infra/deploy/deploy.sh <sha>`:

1. Refuses to start if `.env.prod` is missing (there is no default — a stack that boots with fixture
   credentials would be worse than one that does not boot), if another release holds
   `.release/lock`, or if the checkout is not at the SHA being deployed. The workflow does
   `git fetch --tags origin && git checkout --detach <sha>` first; a hand-run deploy has to do the
   same, because the images are pinned by SHA but the Compose file and the Makefile come from the
   tree.
2. Records the release under way in `/opt/movielens/.release/current` and the one it replaces in
   `.release/previous` — **before** anything changes, so that a box which loses power mid-release
   still names the release whose migrations may already have applied.
3. `make prod-pull` at `IMAGE_TAG=<sha>`, which pulls every image the Compose model names **and then
   asserts each one is present**. Without that assertion a failed pull would be quietly repaired by
   `up` building from the checkout — a release running something CI never tested.
4. `make prod-release`: role provisioning, realm provisioning, migrations and seed, then feature
   materialization. Before the new containers start, because the schema has to be ahead of the code
   that queries it.
5. `make prod-serve` (`up -d --wait`, which for the sidecar means warm rather than merely
   listening), then `make prod-verify`.
6. On any verification failure, rolls back to `.release/previous`, brings that up, and verifies
   again. It exits non-zero on any failure and prints `DEPLOY-OK <sha>` or `ROLLBACK-OK <sha>` as its
   last line; the workflow fails loudly if neither appears. If the *rollback* fails to verify it
   prints `ROLLBACK-FAILED` and stops — that is the case that needs a human, and it is deliberately
   not retried.

Two timings so nothing looks hung: **Keycloak provisioning takes 1.5–3 minutes**, not seconds —
every `kcadm` call starts a JVM and a first run makes 75–85 of them — and the **model-server warms
every worker inside `lifespan` before `/healthz` returns 200**, so it is deliberately the slowest
service to become healthy.

What provisioning creates: realms `demo` and `default` (matching the two rows migration `0002` seeds
into `public.tenants` — **provisioning a tenant means creating both a realm and a DB row, and either
alone fails closed**); clients `movielens-api`, `movielens-web` and `movielens-verify` with the
`oidc-audience-mapper` on each; and exactly three accounts — `walkthrough` (realm `demo`,
`demo-impersonator`), `verify` (realm `demo`, `demo-impersonator`) and `isolation` (realm `default`,
**deliberately without** the role, so the cross-tenant canary has a subject that must be denied). It
prints `PROVISION-OK`. An account that already exists keeps its password unless
`KC_RESET_PASSWORDS=true`.

**The bootstrap admin cannot be deleted after the first deploy, and this is worth internalising
before you try.** Provisioning is idempotent and runs on *every* release, authenticating with
`KEYCLOAK_ADMIN` / `KEYCLOAK_ADMIN_PASSWORD`, and both `keycloak` and `keycloak-provision` declare
that password as required (`${VAR:?}`) — so removing it from `.env.prod` fails the next deploy at
Compose interpolation, before a single container starts. Treat it instead as a long-lived generated
credential that exists only in `.env.prod` (0600) and the password manager, and never sign in with
it: the named human admin from `KC_HUMAN_ADMIN_USERNAME` / `KC_HUMAN_ADMIN_PASSWORD` is the console
account, and the `verify` job uses the same identity for its realm-invariants row. Making the
console account genuinely disappear means giving provisioning a service-account client instead —
a code change, not a variable change, and not one to attempt during a first deploy.

**Keycloak prints `WARN … The following used options or option values are DEPRECATED … - hostname -
hostname-strict` on every boot.** It is informational and not a misconfiguration. Do not "fix" it
during an incident.

Realm changes made in the console or by `kcadm` do not travel back to git on their own. Export them
into `infra/keycloak/realms/` or the templates drift permanently; a realm export carries generated
ids and timestamps, so read the diff semantically (realm flags, client ids and their grant flags,
redirect URIs, mapper presence and its audience) rather than textually.

## 8. Verify

```bash
ssh deploy@<ip> 'cd /opt/movielens && make prod-verify'
```

It runs `python -m src.release.verify --all` from inside the network and prints `VERIFY-OK`, then the
reliability harness. What it covers:

- Issuer equality on the live `demo` discovery document.
- Cold-start and learned serving: four persona slugs present; Action Fan has history **and**
  recommendations with policy exactly `item-item-cosine+lightgbm`; Cold Start has no history but does
  get recommendations with policy exactly `popularity`; zero overlap between seen and recommended.
- `serving_policy.learned === true` for a warm persona — the check that catches a silent popularity
  fallback returning HTTP 200.
- The auth boundary: 401 unauthenticated across nine routes, plus request-id echo and persistence,
  dependency visibility, degraded metadata, bounded pages, cursor rejection, and rate limiting — the
  `X-RateLimit-*` headers on an admitted request and a `429` with `Retry-After` once a bucket is
  drained, with no third behaviour; and, since the shared bucket landed, that there is exactly *one*
  bucket behind the service — no more than one bucket's worth admitted before the refusal, and a
  burst of brand-new connections refused afterwards. Both bounds are arithmetic against the capacity
  and refill the `429` itself advertised, so changing `RATE_LIMIT_*` needs no change here.
- Tenant isolation: the `default`-realm `isolation` account must be 403 on every persona-guarded
  route, and a `demo` token must be refused against a `default` user id. **An unreachable target is a
  hard failure, never a skip.**
- One write round trip: an idempotent `PUT` with `expected_revision`, an authenticated read asserting
  the committed revision, then a revert — **on Eclectic Viewer (900000103) only. Never Cold Start
  (900000104)**, whose zero-signal state the browser suite depends on.
- Realm invariants, the audit SLI (one JSON line from the last 24 h of `recommendation_audits`), and
  artifact provenance from the sidecar's `/healthz`.

The two stages authenticate as the same `verify` account, and under [ADR
0014](adr/0014-request-rate-limiting.md) that means one token bucket, which is why the target pauses
between them. `.github/workflows/production-canary.yml` runs this same command every thirty minutes.

The latency canary is separate, manual and advisory: `make prod-load` (or the canary workflow's
`run_loadcheck` input) runs k6 at ~5 arrivals/s for 60 s. **Correctness thresholds and the
warm-traffic learned assertion are enforced; p99 is recorded with no verdict.**
`synthetic/load/thresholds.js` in CI remains the SLO's only authority — four lines, never edited. A
canary regression opens an investigation and a CI re-run; it never re-baselines anything. Note also
that k6 runs on the same two vCPUs as the service, so a heavier profile would be measuring
contention it created.

Two things about `make prod-load` that are easy to trip over. It authenticates as the same `verify`
account `make prod-verify` uses, and `prod-verify` deliberately ends by draining that account's
rate-limit bucket; run straight afterwards, the first ten seconds or so of the canary will be
refused with 429s that have nothing to do with the service — wait a quarter of a minute, or read the
refusals as what they are. And the k6 scripts are baked into the `loadcheck` image at build time,
so a local edit under `synthetic/load/` does nothing until `docker compose --profile jobs build
loadcheck`; on the box CI republishes the image on every merge, so this only bites a local
rehearsal.

One maintenance note that will surprise whoever adds the next endpoint: a new persona-guarded route
must be added to `PERSONA_ROUTES` in `synthetic/tenant_isolation/remote_canary.py`, or a unit test
fails by design. A guarded route absent from the canary is a route no deployment proves is isolated.

## 9. Rollback

The deploy path rolls back on its own when verification fails. To do it by hand:

```bash
ssh deploy@<ip> 'cd /opt/movielens && make prod-rollback'      # or: infra/deploy/deploy.sh --rollback
```

It reads `/opt/movielens/.release/previous`, pulls (or reuses) that SHA's images, brings the stack up
on them, and verifies. The previous release's images are on disk until the weekly prune, so a
rollback does not depend on the registry being reachable. Two properties worth knowing before you
need them: a successful rollback makes the restored release `current` and **clears** `previous`, so a
second `--rollback` cannot reinstate the release you just rolled away from; and the checkout itself
is left at the deployed SHA — only the images go back — because rewinding the tree under a running
script would be rewriting the program mid-execution. To rewind the tree as well, check the older SHA
out and deploy it forwards. The workflow also uploads the recorded
previous/current SHAs as a `rollback-target` artifact on every deploy, which is where to look if the
box is unreachable and you need to know what was running.

**Additive migrations only, and the database stays where it is.** A rollback moves images, never the
schema. The rule that makes that safe: the release path runs on **every** deploy including a
rollback, so its schema step compares the database's revision against the revisions its own image
knows about and **exits 0 when the database is ahead**. Without that, rolling back to an older image
raises "Can't locate revision" and turns one incident into two. `make prod-rollback-rehearsal` is the
test of exactly that property. A release that must ship a destructive migration has a database
restore as its rollback path, not a redeploy, and needs a rehearsed restore before it merges.

### 9.1 A rollback that leaves the database ahead

This is the normal case and it needs no action. The old image writes the column set it knows about;
every column a later migration added is nullable with no server default, so the insert succeeds and
the new columns stay NULL — which reads as "this row is older than the question" rather than as a
false measurement. Verified on staging at revision `0019_audit_retrieval_provenance` by inserting a
`recommendation_audits` row with no `retriever_family` / `retriever_sha256` / `ranker_route` /
`encoder_ms`: `INSERT 0 1`.

Migrations `0010`'s backfill and `0012`'s audit columns are still not safely reversible against live
data. Neither is `0018` or `0019` — see below for what each costs, measured rather than assumed.

### 9.2 Rolling the schema back across `0019` and `0018`

**Do not do this on the box.** Both downgrades execute cleanly and both destroy data, and neither is
needed by a rollback: §9.1 is what ADR 0013 performs. This section exists because the reverse path
had never been executed anywhere, and now it has.

Measured 2026-09-05 against the staging stack (`movielens-staging`, Postgres 16.14, macOS 26.5.2 /
arm64, Docker 29.6.1), starting from `0019_audit_retrieval_provenance` with three
`recommendation_audits` rows across tenants `demo` and `default` and populated TMDB tables.

Both steps in one command, from the `release` service so Alembic runs as `migrator` — the role that
owns the tables and can therefore alter one under forced RLS. `--no-deps` keeps it to Postgres; the
entrypoint execs an unrecognised mode as given, which is how `alembic` reaches the CLI at all.

```bash
docker compose -p movielens-staging \
  -f docker-compose.prod.yml -f docker-compose.staging.yml --env-file .env.staging \
  run --rm --no-deps -T release alembic downgrade 0017_request_audits
```

```
INFO  [alembic.runtime.migration] Will assume transactional DDL.
INFO  [alembic.runtime.migration] Running downgrade 0019_audit_retrieval_provenance -> 0018_tmdb_catalog, ...
INFO  [alembic.runtime.migration] Running downgrade 0018_tmdb_catalog -> 0017_request_audits, ...
```

Exit 0. `alembic downgrade 0018_tmdb_catalog` then `alembic downgrade 0017_request_audits` produces
the same result in two steps. Postgres runs the whole thing in one transaction, so a failure would
have left the revision where it started; none occurred.

On the box the same invocation is `docker compose -p movielens-prod -f docker-compose.prod.yml
--env-file .env.prod run --rm --no-deps -T release alembic ...`. There is deliberately no Make target
for it: a downgrade should cost a full command line and a look at which project name is in it.

**What each step costs.**

| Step | Structure | Data |
|---|---|---|
| `0019` → `0018` | Drops `ck_audit_encoder_ms`, then the four provenance columns | Every value in `retriever_family`, `retriever_sha256`, `ranker_route`, `encoder_ms` is destroyed. The audit **rows** survive — 3 before, 3 after |
| `0018` → `0017` | Revokes the `app_user` / `admin_user` grants, drops 10 named indexes and all 12 `tmdb_*` tables | Every TMDB catalog row is destroyed. `movies` is untouched — the cascade runs from `movies` into `tmdb_movies`, not the other way |

**What survives, checked explicitly.** `recommendation_audits` keeps `relrowsecurity = t`,
`relforcerowsecurity = t`, owner `migrator`, the `recommendation_audits_tenant_isolation` policy with
both its `USING` and `WITH CHECK` expressions, and its `app_user` (SELECT, INSERT) / `admin_user`
(SELECT, INSERT, UPDATE, DELETE) grants — at every revision in the cycle. Read as `app_user` at
`0017`, `SET LOCAL app.tenant_id = 'demo'` returned 2 rows, `'default'` returned 1, and no setting at
all returned 0. **The rollback does not weaken tenant isolation.**

**Re-upgrading restores the schema exactly.**

```bash
docker compose -p movielens-staging \
  -f docker-compose.prod.yml -f docker-compose.staging.yml --env-file .env.staging \
  run --rm --no-deps -T release alembic upgrade head
```

A column-by-column diff of `information_schema.columns`, `pg_constraint`, `pg_policy` and
`role_table_grants` against the pre-downgrade capture is identical but for one thing:
`ordinal_position` for the four columns moves 31–34 → 35–38, because Postgres leaves a tombstone in
`pg_attribute` for a dropped column and never reuses the number. Column *order* as `SELECT *` returns
it is unchanged, so nothing that names its columns notices. Each full cycle adds four tombstones
against the hard 1600-column ceiling — irrelevant in practice, worth knowing before someone loops it.

**What does not come back is the data.** After `upgrade head` the three audit rows are all still
there and all four provenance columns are NULL on every one of them, including the two that carried
`item-item` / `sasrec`, a real artifact checksum and a measured `encoder_ms`. The TMDB tables come
back with the right shape and zero rows. Nothing in the database can reconstruct either. The
provenance is gone for good — a rolled-back window is a hole in the audit trail that no re-ingest
fills, which is the single strongest reason not to do this. The TMDB catalog is recoverable, because
it is a re-fetchable public snapshot: `make tmdb-load` if the on-disk snapshot survives (it needs no
API token), otherwise `make tmdb-ingest` first, which is roughly 52 minutes at the default rate and
gives every row a new `pulled_at`.

**The failure mode to actually fear is the mismatched pair.** Downgrading the schema while the
current image is still serving is not a degraded path, it is an outage. Every request commits a
durable audit row *before* it answers, and the current writer names all four columns:

```
ERROR:  column "retriever_family" of relation "recommendation_audits" does not exist
```

Every recommendation request fails. If you have downgraded the schema, the matching image must
already be the one running, and `/recommendations` must be checked before you walk away.

**If you rolled back across these revisions anyway, check:**

1. `alembic current` — the revision you intended, on the database you intended.
2. `SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname='recommendation_audits';`
   → `t | t`, and the `recommendation_audits_tenant_isolation` policy present.
3. The `app_user` / `admin_user` grants on `recommendation_audits`, and on the 12 `tmdb_*` tables if
   you re-upgraded.
4. `make prod-verify`, then the isolation canary — grants and RLS are what it exercises.
5. `SELECT count(*) FROM tmdb_movies;` — a zero here means the snapshot needs reloading. Nothing on
   the serving path reads these tables (the product's detail pages read `movie_catalog_metadata`,
   which `0018` does not touch), so this is not a user-visible outage — it is the ADR 0017 increment
   2 feature input going missing, and the next training run is what would find it.
6. The window you rolled through in `recommendation_audits`: those rows now claim nothing about which
   retriever answered. Say so wherever a promotion decision cites them.

**Personas reset to seed state on every release.** The seeder is delete-then-insert over nine known
user ids in tenant `demo`. That is what makes the smoke assertions deterministic, and it also wipes
any movie state a visitor created. It is user-visible; say so on the sign-in door rather than only
here.

## 10. Backups, off-box storage, and the restore drill

`movielens-backup.timer` runs the `backup` job nightly at 04:00 UTC: `pg_dump -Fc --no-owner` of both
databases, `age`-encrypted client-side, copied with `rclone` to
`<remote>:<BACKUP_REMOTE_PATH>/<db>/{daily,weekly,monthly}/<ts>.dump.age`. Retention is 7 daily / 4
weekly / 6 monthly; the weekly and monthly copies are server-side copies of the daily object, so a
longer window costs one API call rather than a second upload. The final line is `BACKUP-OK`.

**Point it at a different provider than the compute.** Both dumps together are megabytes, so either
free tier is sufficient for years:

- **Backblaze B2** (10 GB free). Create a bucket and an application key, then in `.env.prod`:
  ```
  RCLONE_CONFIG_REMOTE_TYPE=b2
  RCLONE_CONFIG_REMOTE_ACCOUNT=<keyID>
  RCLONE_CONFIG_REMOTE_KEY=<applicationKey>
  BACKUP_REMOTE_PATH=<bucket>/recsys
  ```
- **Cloudflare R2** (10 GB free). R2 speaks S3, so:
  ```
  RCLONE_CONFIG_REMOTE_TYPE=s3
  RCLONE_CONFIG_REMOTE_PROVIDER=Cloudflare
  RCLONE_CONFIG_REMOTE_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
  RCLONE_CONFIG_REMOTE_REGION=auto
  RCLONE_CONFIG_REMOTE_ACCESS_KEY_ID=<access-key-id>
  RCLONE_CONFIG_REMOTE_SECRET_ACCESS_KEY=<secret>
  BACKUP_REMOTE_PATH=<bucket>/recsys
  ```

Those two recipes are the whole forwarded set: `TYPE`, `ACCOUNT`, `KEY`, `PROVIDER`, `ENDPOINT`,
`REGION`, `ACCESS_KEY_ID`, `SECRET_ACCESS_KEY`. Compose does not hand a container a variable merely
because `.env.prod` defines it, so a key outside that set needs a line in
`docker-compose.prod.yml`'s `backup` service before rclone ever sees it — otherwise the job resolves
the remote type, finds no credentials, and fails at 04:00 having been configured exactly as
written here.

`BACKUP_AGE_RECIPIENT` is an `age` **public** key (`age-keygen -o backup-key.txt`). The private half
is what must never live on the box or at the backup provider — it goes in the password manager, and a
backup you cannot decrypt is not a backup. Verify a run by hand once:

```bash
ssh deploy@<ip> 'cd /opt/movielens && make prod-backup'
systemctl list-timers movielens-backup.timer
journalctl -u movielens-backup.service -n 50
```

Two details worth knowing before you need them: the dump is `--no-owner` but **not**
`--no-privileges` (stripping privileges would strip the `app_user` / `admin_user` grants migrations
`0002`, `0003` and `0006` install, leaving a database the API cannot read at all), and the dumping
role must hold BYPASSRLS or SUPERUSER (tenant-scoped tables are `FORCE ROW LEVEL SECURITY` and
`pg_dump` sets no `app.tenant_id`, so a dump taken by an ordinary role exits 0 and contains no rows —
the script asserts this before dumping).

**Redis is deliberately not backed up.** It holds only derived state, the repair path is
`bootstrap materialize`, and an empty Redis fails the model-server's boot rather than silently
serving zeros.

`infra/backup/restore-drill.sh` is a script with an exit code, not a document. It pulls the latest
dump, decrypts, restores into a scratch database, asserts the alembic revision matches the image's
head, optionally boots the API image against it and runs the smoke, and writes a JSON record (dump
object, bytes, TOC entries, alembic head, per-stage and total seconds, outcome) to stdout and to
`--report PATH` — **including on failure**, which is the run worth having evidence of. `--dry-run`
rehearses it safely. Run it for real at least once, and after any change to the backup path.

Five things the drill will tell you the hard way if you skip them:

- **Restore as `migrator`, not as the superuser.** The dump is `--no-owner`, so every restored object
  is owned by whatever role ran `pg_restore`. On a normal deployment `migrator` owns every base table
  — `create_tables()` runs as `migrator` before Alembic does, which is what makes
  `ALTER TABLE … FORCE ROW LEVEL SECURITY` and the `0010` backfill work — and a restore performed as
  the superuser silently moves all of it. The restored database *serves* fine; the **next deploy** is
  what breaks, because `alembic upgrade head` runs as `migrator` and `migrator` can no longer read
  `public.alembic_version`. Repairing afterwards with `REASSIGN OWNED BY <restoring role> TO
  migrator;` works too; doing neither leaves a database that passes every check you would think to
  run.
- The restore target needs the §5 provisioning roles **before** `pg_restore`, because the dump
  carries their grants.
- The drill refuses a non-empty target outright, with no override flag.
- The drill's own connection must hold BYPASSRLS or SUPERUSER, or its row counts read zero and an
  empty restore looks successful.
- Booting the API against a restored database needs a pgBouncer alias pointing at it. Without one,
  run with `--skip-api-smoke`, which prints `RESTORE-DRILL-PARTIAL` instead of `RESTORE-DRILL-OK` —
  record the partial as a partial.

**The seed step is deliberately skipped.** A restore that needs the seeder to look right has proven
nothing. Record the wall-clock time and everything done by hand; anything done by hand becomes a
ticket.

If the Postgres image ever moves to a new major version, `infra/backup/Dockerfile`'s `FROM
postgres:16` must move with it or every dump fails `pg_dump`'s own version check.

## 11. An external uptime check

There is no platform healthcheck watching this box. The nightly verify and the 30-minute canary are
the only recurring signals the deployment has, and both run inside GitHub Actions — so if the machine
is off, they fail rather than page. Add one free external checker (UptimeRobot, Better Stack,
Cloudflare's health checks — any of them) on **two** URLs, at 5-minute intervals:

| URL | What a failure means |
|---|---|
| `https://app.<domain>/` | The edge, its certificate and the web app. This is what a visitor sees |
| `https://auth.<domain>/realms/demo/.well-known/openid-configuration` | Keycloak is up *and* the serving realm exists. Unauthenticated by design, returns JSON, and catches the case where the site loads but nobody can sign in |

Alert to somewhere you actually read. Two things worth configuring while you are there: **certificate
expiry** warnings (Caddy renews automatically, but a renewal that has been failing for a month is
silent until the day it is not) and a **response-body keyword** on the discovery URL, so a 200 from a
captive edge does not read as healthy.

## 12. Disk, logs and memory housekeeping

Monthly, or whenever something looks off:

```bash
df -h /                                    # 40 GB total. Investigate under ~8 GB free
docker system df                           # images vs volumes vs build cache
docker stats --no-stream                   # against the limits in docker-compose.prod.yml
systemctl list-timers movielens-*          # both timers still armed
journalctl -u movielens-prune.service -n 20
```

- **Logs** are capped by `/etc/docker/daemon.json` at 20 MB × 5 per container — a hard ceiling, not a
  cron job. If a container is noisy enough to matter, the fix is the container, not the ceiling.
- **Images** are pruned weekly, older than seven days, **volumes never**. That window is deliberate:
  it is what keeps the previous release rollback-able without the registry.
- **`feature_store.*` grows one generation per release and nothing prunes it** (D8). On a fixed disk
  that is a slow leak. Check it with a row count when disk gets tight, before blaming images.
- **Memory is the constraint, not disk.** If a container disappears, check `docker inspect
  <name> --format '{{.State.OOMKilled}}'` **before** reading application logs — an OOM kill and a
  crash look identical from inside the application.
- **Reboots** (kernel or Docker upgrades) are a full outage; `movielens.service` brings the stack
  back, but do them deliberately rather than discovering one during a demo.

## 13. Incident quick reference

| Symptom | First response |
|---|---|
| **Recommendations look uniformly wrong** — same titles for everyone, or nothing personal | `docker compose … run --rm materialize` (or `python -m src.release.bootstrap materialize`). This is the first response, before reading any code. An empty or stale online store is by far the likeliest cause |
| `model-server` crash-looping with a missing Feast event timestamp or an all-zero feature frame | **Materialize, do not roll back.** The sidecar refuses to boot against an unmaterialized Redis on purpose — that is the check working |
| A container vanished | `docker inspect <name> --format '{{.State.OOMKilled}}'`. On a 4 GB box this is the first hypothesis, not the last |
| Every request 401s, on every route, immediately | The issuer disagrees somewhere. `PUBLIC_AUTH_HOST` is the single source; compare what the token carries against `https://<PUBLIC_AUTH_HOST>/realms/demo` character by character, including scheme and trailing slash |
| The browser shows a certificate error | ACME. Check `docker compose logs edge` for the challenge, then that both A records still point here. Do not read an auth failure behind a bad certificate as an auth problem |
| A container refuses to start with "the default model-server token is only permitted in development" | `MODEL_SERVER_AUTH_TOKEN` is missing on a service that constructs `Settings()` — that includes `model-server`, `release` and `verify`, none of which use the token to talk to anything |
| Every recommendation reports `learned: false` with `no-champion` or `champion-mismatch` | The tenant's champion columns on `public.tenants` (migration 0016) and the bundle the sidecar loaded do not agree. `no-champion` means the row names none — the migration seeds `demo` and deliberately leaves `default` and `synth_cold` empty, so this on `demo` means the seeding update was skipped or the row was edited. `champion-mismatch` means a promotion moved the row without shipping the image, or the reverse; `V-12` in `make prod-verify` compares all three against the sidecar's `/healthz` and names which coordinate disagrees. Fix the row or deploy the matching image — the service keeps serving popularity meanwhile |
| The API boots but recommendations report `learned: false` with `model-server-unavailable` | The two `MODEL_SERVER_AUTH_TOKEN` values disagree, or the sidecar is not warm. A token mismatch degrades every recommendation to popularity **at HTTP 200** rather than erroring — rotate that value in a window and let verify confirm recovery |
| The API refuses to boot on a pgBouncer check | Either the pooler is down (the API cannot boot without it, by design), or `pool_mode` is not `transaction`, or `PGBOUNCER_ADMIN_PASSWORD` disagrees between the API and the pooler |
| A deploy never finishes and readiness times out | `/readyz` is returning non-200. It fails on database or JWKS only, never on a sidecar — so this means the pooler path or Keycloak, not the model server |
| `make prod-verify` fails but the site looks fine | Read the failing row before anything else. A silent popularity fallback, a broken isolation guard and a stale audit table all look fine from a browser |
| Requests come back `429` with `Retry-After` | The ADR 0014 token bucket, working. It is keyed on `(tenant, sub)` from the verified token, so it is one *account* that is over, not the deployment. One bucket in Redis serves every worker — 600/minute with a burst of 120 for the whole service, not per process. If the workload is legitimate, raise `RATE_LIMIT_REQUESTS_PER_MINUTE`; never reach for `RATE_LIMIT_ENABLED=false` on a public service, and never exempt a client |
| `/readyz` reports `rate_limit: degraded` | The shared bucket cannot reach Redis and is failing open onto a per-worker one, so the limit is now `workers ×` what it says. Deliberate (a limiter is backpressure, not an auth boundary, and failing closed would turn a Redis blip into an outage), and reported here because a `429` writes no audit row and the API runs with `--no-access-log`, so nothing else records it. Check `docker compose logs redis` and the API's `outcome=fail_open` lines, which carry the count they stand for |
| The reliability harness fails with "about N buckets" or "brand-new connections were admitted" | The service is enforcing one bucket per worker rather than one bucket. Either `RATE_LIMIT_BACKEND` is `memory` on `api` (it should be absent, and the default is `redis`), or the shared bucket is failing open — see the `/readyz` row above |
| A deploy failed and the workflow says the host rolled back | The previous release is running and was verified. Read the deploy log's verify rows to find what the new commit broke; the box is not the thing to debug |

## 14. What this deployment does not do

Stated plainly so nobody reads an aspiration as a description of what is running:

- **There is no high availability.** One machine, one of everything. A reboot, a full disk or a
  hardware failure is a full outage, and a deploy is a brief one.
- **Audit coverage is two tables and no retention policy.** Every authenticated request writes a row:
  `recommendation_audits` for the recommendation route (predictions, features, versions, input
  digests) and `request_audits` for everything else (tenant, actor, persona, route template, method,
  status, outcome, latency, correlation id). Both are forced-RLS and both commit before the response.
  What the deployment does *not* have is pruning — `request_audits` grows by one row per
  authenticated request forever, the same open question `feature_store.*` has. `REQUEST_AUDIT_MODE=off`
  turns the generic writer off without a migration if that ever becomes urgent before D8 is settled.
  Two things the generic table deliberately does not store: request bodies and query strings, so a
  viewer's search terms never land in it. Rows that address no persona (`/whoami`, `/personas`) carry
  a null `user_id` and are readable only as `admin_user` — `GET /users/{id}/request-audits` is
  persona-scoped, and there is no tenant-wide operator view (Grafana's job, Phase 5).
- **Feature parity has no production form.** `tests/feature_parity/` stays a CI gate; pointing it at
  production would mean giving a runner production database and Redis credentials and letting it
  write demo data.
- **The pinned latency gate has no production form.** Its wrapper needs Compose recreation, `docker
  stats`, cgroup reads and a `/proc/stat` probe. CI keeps the verdict; the canary is a weaker
  instrument and says so.
- **There is no `/me` ownership mapping.** Any signed-in `demo`-realm account can read and mutate all
  four personas. That is why registration is disabled and only three accounts exist.
- **There is no metrics endpoint.** `/metrics` is deliberately not added — a scrape endpoint carries
  per-tenant counts and latencies and there is no auth story for a scraper yet. Phase 5 owns it,
  along with the Grafana that would consume it.
- **`feature_store.*` is never pruned.** One generation per release, and `user_item_features` is a
  users × movies cross join. That was 960 rows a generation while the catalog held 120 titles; since
  the catalog became the full MovieLens 62,423 it is 499,384 rows a generation (8 rated users), and
  every release appends another. No longer merely unbounded in principle — D8 is the decision that
  fixes it, and it now has a schedule.
