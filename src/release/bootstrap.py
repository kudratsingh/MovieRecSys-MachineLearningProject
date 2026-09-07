"""Bring a deployed environment up to the state the services expect.

Five subcommands, each of which is one step of the release sequence that used
to live in ``make`` recipes coupled to Compose:

``preflight``
    Refuse to go further if the environment is not the one this code was built
    for. Everything here is read-only and cheap, and every assertion is one a
    later step would otherwise discover expensively: the API crash-looping on a
    CSV-shaped variable, every request 401ing on an issuer mismatch, Redis
    silently evicting the online feature store.

``schema``
    ``create_tables`` then ``alembic upgrade head``, in that order — migration
    0003 adds a column to ``ratings``, so the base tables have to exist first
    and ``alembic upgrade head`` alone is not a bootstrap. **If the database's
    revision is unknown to this image, this step applies nothing and exits 0.**
    A pre-deploy command runs on every deploy including a rollback, and an
    older image's ``upgrade head`` against a newer database raises "Can't
    locate revision" — turning one incident into two.

``seed``
    The demo tenant's personas and catalog. Idempotent by construction, and
    therefore a reset: a release returns the four personas to their seeded
    state, which is what makes the smoke assertions deterministic and is
    user-visible.

``materialize``
    The model-server's pre-deploy. Fences on the schema being in place (there
    is no cross-service ``depends_on`` on a PaaS), publishes the offline
    snapshots into Postgres and Redis, then proves two things about what it
    published: that the registry the apply produced still describes the feature
    views this image's code declares *and* the one baked into the image, and
    that a real persona reads back a non-default feature frame. **Like
    ``schema``, a database whose revision this image has never heard of is read
    as ahead rather than as missing**, so a rollback's pre-deploy is satisfied
    immediately instead of waiting out its whole deadline.

``all``
    ``preflight`` then ``schema`` then ``seed``. Not ``materialize`` — that one
    runs in the features image, next to Feast, and needs the schema this step
    creates.

**Imports are deliberately lazy.** The same ``src/`` tree is baked into two
images with different dependency sets: the slim API image ships no Feast,
pandas or LightGBM, and the features image ships no httpx. A module-level
import of either side's dependencies would make this module unimportable in the
other image, so each subcommand imports what only it needs. Alembic is the one
dependency both images carry — the API image to *run* migrations, the features
image only to read the revision graph its fence compares against.
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import ssl
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from src.config import Settings
from src.release import RELEASE_BOOTSTRAP_SENTINEL
from src.serving.startup_checks import StartupCheckError, run_startup_checks

if TYPE_CHECKING:  # pragma: no cover - import-time typing only
    from alembic.config import Config

logger = logging.getLogger("release.bootstrap")

# ``src/release/bootstrap.py`` -> ``src/release`` -> ``src`` -> the tree root,
# which is ``/app`` in both images and the repository root in a checkout. Paths
# are resolved from here rather than from the process's cwd because a platform
# start command decides that cwd, and this module is invoked as both a
# pre-deploy command and a job.
TREE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ALEMBIC_INI = TREE_ROOT / "alembic.ini"

FEATURE_STORE_SCHEMA = "feature_store"
FEATURE_STORE_TABLES = ("user_features", "item_features", "user_item_features")

# Redis is the sole online feature store, not a cache: the feature views carry
# 3650-day TTLs and a missing feature reads back as 0.0 rather than as an
# error, so an eviction silently degrades every ranking score with nothing
# failing anywhere.
REQUIRED_MAXMEMORY_POLICY = "noeviction"

# Action Fan. Named here rather than imported because ``synthetic/`` is not
# copied into the features image, which is where ``materialize`` runs.
DEFAULT_PERSONA_USER_ID = 900000101

_FENCE_POLL_SECONDS = 2.0
_DEFAULT_TIMEOUT_SECONDS = 10.0


class ReleaseError(RuntimeError):
    """A release step could not establish what it exists to establish."""


class PreflightError(ReleaseError):
    """A precondition for deploying into this environment does not hold."""


class SchemaFenceTimeoutError(ReleaseError):
    """The schema this step depends on did not arrive within the deadline."""


class SchemaToolingUnavailableError(ReleaseError):
    """This image cannot enumerate its own migrations."""


# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------


def check_environment_is_production(settings: Settings) -> str:
    """Refuse to bootstrap anything that is not the production environment.

    ``Settings`` types ``environment`` as a Literal, so a typo is already a
    construction failure. What this adds is the direction: a release job that
    inherited ``dev`` would run every dev-credential default against a real
    database, and the guards in ``Settings.__init__`` are all conditioned on
    exactly the value this asserts.
    """
    if settings.environment != "production":
        raise PreflightError(
            f"release bootstrap refuses to run with environment={settings.environment!r}; "
            "set ENVIRONMENT=production on this job. Every dev-credential guard in "
            "src/config.py is conditioned on that value."
        )
    return settings.environment


def check_authorized_parties(settings: Settings) -> list[str]:
    """``KEYCLOAK_AUTHORIZED_PARTIES`` must be a JSON list, and must mean it.

    pydantic-settings parses the field as JSON, so the CSV form raises at
    construction and crash-loops the API. A variable panel is exactly where
    somebody types the comma, so this catches the near-miss too: a
    single-element list whose one element is itself a comma-separated string is
    valid JSON, constructs fine, and rejects every real token.
    """
    parties = [party for party in settings.keycloak_authorized_parties]
    if not parties:
        raise PreflightError(
            "KEYCLOAK_AUTHORIZED_PARTIES is empty; every access token would be rejected "
            'by azp. Set it as a JSON list, e.g. ["movielens-api","movielens-web"].'
        )
    for party in parties:
        if not party.strip():
            raise PreflightError(f"KEYCLOAK_AUTHORIZED_PARTIES contains a blank entry: {parties!r}")
        if "," in party:
            raise PreflightError(
                f"KEYCLOAK_AUTHORIZED_PARTIES entry {party!r} contains a comma, so the "
                "value was written as CSV inside a JSON string. It is a JSON list of "
                'client ids: ["movielens-api","movielens-web","movielens-verify"].'
            )
    return parties


@dataclass(frozen=True)
class Endpoint:
    """One host:port this deployment has to be able to open a socket to."""

    name: str
    host: str
    port: int

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


def check_tcp_reachable(endpoint: Endpoint, *, timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> str:
    """Open and immediately close a TCP connection, or say which one failed.

    Deliberately below the protocol level: a DSN typo, a private-network name
    that does not resolve and a service that has not finished starting all
    present here as one clear failure instead of as a driver-specific error
    three steps later.
    """
    try:
        with socket.create_connection((endpoint.host, endpoint.port), timeout=timeout):
            pass
    except OSError as exc:
        raise PreflightError(
            f"{endpoint.name} is not reachable at {endpoint}: {type(exc).__name__}: {exc}"
        ) from exc
    return str(endpoint)


def parse_redis_connection_string(connection_string: str) -> tuple[Endpoint, dict[str, str]]:
    """Split Feast's ``host:port,key=value,...`` form into an endpoint and options.

    Feast's Redis online store does not take a ``redis://`` URL, so this is the
    only shape the deployment ever holds and the only one worth parsing.
    """
    parts = [part.strip() for part in connection_string.split(",") if part.strip()]
    if not parts:
        raise PreflightError("REDIS_CONNECTION_STRING is empty")
    host_port = parts[0]
    options: dict[str, str] = {}
    for part in parts[1:]:
        key, separator, value = part.partition("=")
        if not separator:
            raise PreflightError(
                f"REDIS_CONNECTION_STRING option {part!r} is not a key=value pair; Feast's "
                "form is host:port,password=...,ssl=true"
            )
        options[key.strip().lower()] = value.strip()
    host, separator, port = host_port.partition(":")
    if not separator or not host:
        raise PreflightError(
            f"REDIS_CONNECTION_STRING must start with host:port; got {host_port!r}"
        )
    try:
        return Endpoint("redis", host, int(port)), options
    except ValueError as exc:
        raise PreflightError(f"REDIS_CONNECTION_STRING port {port!r} is not a number") from exc


class _ByteStream(Protocol):
    """The part of a socket file object the RESP reader below uses."""

    def readline(self) -> bytes: ...

    def read(self, size: int, /) -> bytes: ...

    def write(self, data: bytes, /) -> Any: ...

    def flush(self) -> None: ...


def _resp_command(*args: str) -> bytes:
    """Encode one Redis command as a RESP array."""
    chunks = [f"*{len(args)}\r\n".encode()]
    for arg in args:
        raw = arg.encode()
        chunks.append(f"${len(raw)}\r\n".encode())
        chunks.append(raw + b"\r\n")
    return b"".join(chunks)


def _read_resp(stream: _ByteStream) -> Any:
    """Read one RESP reply. Enough of the protocol for AUTH and CONFIG GET."""
    line = stream.readline()
    if not line:
        raise PreflightError("Redis closed the connection before replying")
    prefix, payload = line[:1], line[1:].strip()
    if prefix == b"+":
        return payload.decode()
    if prefix == b"-":
        raise PreflightError(f"Redis refused the command: {payload.decode()}")
    if prefix == b":":
        return int(payload)
    if prefix == b"$":
        length = int(payload)
        if length < 0:
            return None
        body = stream.read(length + 2)
        return body[:length].decode()
    if prefix == b"*":
        count = int(payload)
        if count < 0:
            return None
        return [_read_resp(stream) for _ in range(count)]
    raise PreflightError(f"unrecognised Redis reply: {line!r}")


def redis_config_get(
    endpoint: Endpoint,
    options: dict[str, str],
    parameter: str,
    *,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Read one Redis config parameter over a raw socket.

    A ~40-line RESP client rather than a dependency: the API image is sized for
    the ADR 0008 sidecar split and adding a Redis client to a serving image for
    the sake of one preflight assertion is the wrong trade. The image installs
    no Redis library at all today, so importing one here would make this
    subcommand fail in the only place it runs.
    """
    connection = socket.create_connection((endpoint.host, endpoint.port), timeout=timeout)
    try:
        if options.get("ssl", "").lower() in {"true", "1", "yes"}:
            connection = ssl.create_default_context().wrap_socket(
                connection, server_hostname=endpoint.host
            )
        stream = connection.makefile("rwb")
        password = options.get("password")
        if password:
            username = options.get("username")
            credentials = ("AUTH", username, password) if username else ("AUTH", password)
            stream.write(_resp_command(*credentials))
            stream.flush()
            _read_resp(stream)
        stream.write(_resp_command("CONFIG", "GET", parameter))
        stream.flush()
        reply = _read_resp(stream)
    except OSError as exc:
        raise PreflightError(
            f"Redis at {endpoint} could not be queried: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        connection.close()
    if not isinstance(reply, list) or len(reply) < 2:
        raise PreflightError(f"Redis CONFIG GET {parameter} returned {reply!r}")
    return str(reply[1])


def check_redis_eviction_policy(
    settings: Settings,
    *,
    endpoint: Endpoint | None = None,
    options: dict[str, str] | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Assert Redis will never evict, before anything is materialized into it."""
    if endpoint is None or options is None:
        endpoint, options = parse_redis_connection_string(settings.redis_connection_string)
    policy = redis_config_get(endpoint, options, "maxmemory-policy", timeout=timeout)
    if policy != REQUIRED_MAXMEMORY_POLICY:
        raise PreflightError(
            f"Redis maxmemory-policy is {policy!r}; the online feature store requires "
            f"{REQUIRED_MAXMEMORY_POLICY!r}. Under any eviction policy a dropped feature "
            "reads back as 0.0 and every ranking score degrades with nothing failing."
        )
    return policy


def check_pooler_and_app_role(settings: Settings) -> str:
    """Run the API's own boot assertions against this configuration, first.

    Reuses ``run_startup_checks`` rather than restating it: the point is not
    that some code asserts transaction pooling, it is that *the assertions the
    API will run in a few minutes* hold now, while a failure is a job that
    exited rather than a service that will not promote.
    """
    engine = create_engine(settings.app_user_database_url, pool_pre_ping=True, future=True)
    try:
        run_startup_checks(settings=settings, app_engine=engine)
    except StartupCheckError as exc:
        raise PreflightError(
            f"the API's own startup assertions fail against this configuration: {exc}"
        ) from exc
    finally:
        engine.dispose()
    return "app_user has RLS applied; pgBouncer is in transaction mode"


def _fetch_json(url: str, *, timeout: float) -> dict[str, Any]:
    # httpx is absent from the features image; preflight only ever runs in the
    # API image, so the import belongs to the call rather than to the module.
    import httpx

    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=False)
        response.raise_for_status()
        document = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise PreflightError(f"{url} could not be read: {type(exc).__name__}: {exc}") from exc
    if not isinstance(document, dict):
        raise PreflightError(f"{url} did not return a JSON object")
    return document


def check_issuer_equality(
    *,
    public_base_url: str,
    realm: str,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    fetch: Callable[[str], dict[str, Any]] | None = None,
) -> str:
    """The realm's discovery document must name the issuer this API trusts.

    ``AuthMiddleware`` rebuilds ``{keycloak_public_base_url}/realms/{realm}``
    and rejects any token whose ``iss`` is not exactly that string. There is no
    partial failure mode: get this wrong and every authenticated request 401s
    while every service reports healthy. It is the likeliest first-deploy
    failure in the whole design, and it is knowable before anything deploys.
    """
    read = fetch if fetch is not None else (lambda url: _fetch_json(url, timeout=timeout))
    expected = f"{public_base_url.rstrip('/')}/realms/{realm}"
    document = read(f"{expected}/.well-known/openid-configuration")
    issuer = document.get("issuer")
    if issuer != expected:
        raise PreflightError(
            f"realm {realm!r} reports issuer {issuer!r}, but this deployment trusts "
            f"{expected!r}. Every token minted here would be rejected by the auth "
            "middleware. Check KEYCLOAK_PUBLIC_BASE_URL, KC_HOSTNAME and KC_PROXY_HEADERS."
        )
    return expected


def run_preflight(
    settings: Settings,
    *,
    realm: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Every precondition, in the order that fails cheapest first."""
    resolved_realm = realm or settings.model_tenant_id
    redis_endpoint, redis_options = parse_redis_connection_string(settings.redis_connection_string)
    summary: dict[str, Any] = {"step": "preflight"}
    summary["environment"] = check_environment_is_production(settings)
    summary["authorized_parties"] = check_authorized_parties(settings)
    summary["reachable"] = {
        endpoint.name: check_tcp_reachable(endpoint, timeout=timeout)
        for endpoint in (
            Endpoint("postgres", settings.postgres_host, settings.postgres_port),
            Endpoint("pgbouncer", settings.app_user_db_host, settings.app_user_db_port),
            redis_endpoint,
        )
    }
    summary["pooler"] = check_pooler_and_app_role(settings)
    summary["redis_maxmemory_policy"] = check_redis_eviction_policy(
        settings, endpoint=redis_endpoint, options=redis_options, timeout=timeout
    )
    summary["issuer"] = check_issuer_equality(
        public_base_url=settings.keycloak_public_base_url,
        realm=resolved_realm,
        timeout=timeout,
    )
    return summary


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SchemaOutcome:
    """What ``schema`` did, and to which revision the database now stands."""

    applied: bool
    revision: str
    reason: str


def _alembic_config(config_path: Path) -> Config:
    """Build an Alembic config whose script location does not depend on cwd.

    ``alembic.ini`` sets ``script_location = alembic``, which Alembic resolves
    against the process's working directory. A release job's cwd is whatever
    the platform's start command left it at, so the relative value is made
    absolute against the ini file itself.
    """
    try:
        from alembic.config import Config as AlembicConfig
    except ImportError as exc:  # pragma: no cover - exercised by the features image
        raise SchemaToolingUnavailableError(
            "this image does not ship Alembic, so it cannot read its own migrations"
        ) from exc
    if not config_path.is_file():
        raise SchemaToolingUnavailableError(f"no Alembic config at {config_path}")
    config = AlembicConfig(str(config_path))
    location = config.get_main_option("script_location")
    if location and not Path(location).is_absolute():
        config.set_main_option("script_location", str((config_path.parent / location).resolve()))
    return config


def _script_directory(config_path: Path) -> Any:
    """This image's Alembic script directory, or a named reason it has none.

    Every failure mode collapses to one exception on purpose. Both deployed
    images carry ``alembic`` and ``alembic/`` today, so reaching this failure
    means an image was built without them — which the callers turn into a
    message naming the two COPY lines, rather than into a ``ModuleNotFoundError``
    traceback from inside a pre-deploy command.
    """
    try:
        from alembic.script import ScriptDirectory
    except ImportError as exc:
        raise SchemaToolingUnavailableError(
            "this image does not ship Alembic, so it cannot read its own migrations"
        ) from exc
    try:
        return ScriptDirectory.from_config(_alembic_config(config_path))
    except SchemaToolingUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 - Alembic raises CommandError and OSError here
        raise SchemaToolingUnavailableError(
            f"this image's Alembic script directory could not be read: {exc}"
        ) from exc


def known_revisions(config_path: Path = DEFAULT_ALEMBIC_INI) -> frozenset[str]:
    """Every revision this image's script directory knows about."""
    try:
        script = _script_directory(config_path)
        return frozenset(revision.revision for revision in script.walk_revisions())
    except SchemaToolingUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 - Alembic raises CommandError and OSError here
        raise SchemaToolingUnavailableError(
            f"this image's Alembic script directory could not be read: {exc}"
        ) from exc


def head_revision(config_path: Path = DEFAULT_ALEMBIC_INI) -> str:
    """The revision ``alembic upgrade head`` would take a database to."""
    try:
        head = _script_directory(config_path).get_current_head()
    except SchemaToolingUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 - Alembic raises CommandError and OSError here
        raise SchemaToolingUnavailableError(
            f"this image's Alembic head revision could not be read: {exc}"
        ) from exc
    if head is None:
        raise SchemaToolingUnavailableError("this image's Alembic script directory has no head")
    return str(head)


def database_revisions(engine: Engine) -> tuple[str, ...]:
    """Revisions the database reports, empty when it has never been migrated."""
    if not inspect(engine).has_table("alembic_version"):
        return ()
    try:
        with engine.connect() as connection:
            rows = connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
            return tuple(sorted(str(row) for row in rows))
    except ProgrammingError as exc:
        # 42501 is insufficient_privilege. `alembic_version` is owned by
        # whoever ran the migration and carries no GRANT, so a role that is
        # not the migrator cannot read it — see `wait_for_schema`, which is the
        # one caller that runs as a different role.
        if getattr(exc.orig, "pgcode", None) != "42501":
            raise
        # Which of the two causes it is depends on who is asking, and getting
        # that wrong sends an operator to add a GRANT that is already there.
        # admin_user is the sidecar's pre-deploy fence and wants the default
        # privilege; migrator owns the table on a healthy deployment, so its
        # being refused means ownership moved -- which is what a `pg_restore`
        # run as the superuser does to every table in the dump.
        role = ""
        try:
            with engine.connect() as connection:
                role = str(connection.execute(text("SELECT current_user")).scalar_one())
        except Exception:  # pragma: no cover - diagnosis must not mask the original error
            pass
        remedy = (
            "`migrator` owns public.alembic_version on a deployment that migrated normally, "
            "so being refused here means ownership moved. A `pg_restore --no-owner` run as "
            "the superuser does exactly that: restore as `migrator` instead, or "
            "`REASSIGN OWNED BY <restoring role> TO migrator;` in the restored database."
            if role == "migrator"
            else "Add `ALTER DEFAULT PRIVILEGES FOR ROLE migrator IN SCHEMA public GRANT "
            "SELECT ON TABLES TO admin_user;` to the one-time provisioning SQL, before the "
            "first migration runs."
        )
        raise ReleaseError(
            f"role {role or 'this role'} may not read public.alembic_version, so the "
            f"migration state cannot be established. {remedy}"
        ) from exc


def apply_schema(
    engine: Engine,
    *,
    config_path: Path = DEFAULT_ALEMBIC_INI,
    prepare: Callable[..., None] | None = None,
) -> SchemaOutcome:
    """Create the base tables and migrate, unless the database is ahead.

    The no-op is the whole point of comparing revisions rather than just
    running the upgrade. Pre-deploy commands run on every deploy, rollbacks
    included, and an older image's ``upgrade head`` against a database carrying
    a newer revision raises "Can't locate revision" — so the rollback that was
    supposed to end an incident fails instead. Additive-migrations-only is the
    policy that makes leaving a newer schema in place safe; this is the code
    that leaves it in place.
    """
    current = database_revisions(engine)
    known = known_revisions(config_path)
    ahead = sorted(set(current) - known)
    if ahead:
        return SchemaOutcome(
            applied=False,
            revision=",".join(current),
            reason=(
                f"database revision {', '.join(ahead)} is unknown to this image; it is "
                "ahead of this release, so nothing was applied (rollback path)"
            ),
        )

    from alembic import command
    from src.data.demo_setup import prepare_demo_database

    config = _alembic_config(config_path)

    def upgrade(_config: Config, revision: str) -> None:
        # `prepare_demo_database` builds its own Config from the ini path,
        # which resolves `script_location` against cwd. The ordering it encodes
        # -- create_tables before upgrade -- is what is being reused; the config
        # it constructed is replaced with the cwd-independent one.
        command.upgrade(config, revision)

    (prepare or prepare_demo_database)(engine, config_path=config_path, upgrade=upgrade)
    return SchemaOutcome(applied=True, revision=head_revision(config_path), reason="applied")


# --------------------------------------------------------------------------
# seed
# --------------------------------------------------------------------------


def run_seed(engine: Engine) -> dict[str, Any]:
    """Seed the demo tenant's catalog, personas and background users.

    Idempotent by construction, which makes it a reset as much as a seed: the
    persona and background rows are delete-then-insert scoped to nine known
    ids, so a release returns the four personas to seed state and discards any
    movie state a visitor created on them.
    """
    # `synthetic/` is copied into the API image but not into the features one.
    from synthetic.personas.seed import seed_demo_personas

    result = seed_demo_personas(engine)
    return {
        "step": "seed",
        "tenant_id": result.tenant_id,
        "personas": result.persona_count,
        "persona_ratings": result.persona_rating_count,
        "background_ratings": result.background_rating_count,
        "catalog_movies": result.catalog_movie_count,
        "reviewed_movies": result.reviewed_movie_count,
        "recommendable_movies": result.recommendable_movie_count,
        "poster_movies": result.poster_movie_count,
    }


# --------------------------------------------------------------------------
# materialize
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SchemaState:
    """What the fence can currently see of the schema it is waiting for."""

    revisions: tuple[str, ...]
    revision_ok: bool
    missing_feature_tables: tuple[str, ...]
    tenant_has_ratings: bool
    # Revisions the database reports that this image's script directory has
    # never heard of. Non-empty means the database is ahead of this release.
    unknown_revisions: tuple[str, ...] = ()

    @property
    def satisfied(self) -> bool:
        return self.revision_ok and not self.missing_feature_tables and self.tenant_has_ratings

    def satisfaction(self, *, expected_revision: str) -> str:
        """Why the fence is satisfied — at head, or ahead of this release.

        The two are worth telling apart in a deploy log. "Ahead" is the
        rollback path, and it is the one an operator staring at a deploy needs
        to recognise on sight: nothing is broken, this image is simply older
        than the database it is being deployed against.
        """
        if self.unknown_revisions:
            return (
                f"database revision {', '.join(self.unknown_revisions)} is unknown to this "
                "image; it is ahead of this release, so the fence is satisfied without "
                "waiting (rollback path)"
            )
        return f"database is at this release's expected revision {expected_revision}"

    def unmet(self, *, expected_revision: str, tenant_id: str) -> list[str]:
        reasons: list[str] = []
        if not self.revision_ok:
            observed = ", ".join(self.revisions) if self.revisions else "none"
            reasons.append(
                f"alembic_version is {observed}, not this image's head {expected_revision}"
            )
        if self.missing_feature_tables:
            reasons.append(
                f"{FEATURE_STORE_SCHEMA} is missing "
                f"{', '.join(self.missing_feature_tables)} (migration 0007)"
            )
        if not self.tenant_has_ratings:
            reasons.append(f"tenant {tenant_id!r} has no ratings to build features from")
        return reasons


def resolve_expected_revision(
    explicit: str | None = None,
    *,
    config_path: Path = DEFAULT_ALEMBIC_INI,
) -> str:
    """The head revision this deploy expects the database to be at.

    The image's own script directory is the answer, not a variable somebody
    maintains alongside it. There was a ``RELEASE_EXPECTED_REVISION`` here while
    the features image shipped no Alembic; it is gone because a hand-written
    literal in a variable panel is only ever as correct as the last person to
    remember it, and its failure mode — a pre-deploy fence that waits out its
    deadline and fails the deploy — is the expensive one. ``--expected-revision``
    remains for the rehearsal case where a run wants to pin the comparison.
    """
    if explicit:
        return explicit
    try:
        return head_revision(config_path)
    except SchemaToolingUnavailableError as exc:
        raise ReleaseError(
            "the schema fence needs the revision this release expects, and this image "
            "carries no Alembic script directory. Both deployed images are built with "
            "one: add `COPY alembic.ini ./` + `COPY alembic ./alembic` and the alembic "
            "dependency (infra/features/Dockerfile, infra/features/requirements.txt), or "
            "pass --expected-revision for this run."
        ) from exc


def inspect_schema_state(
    engine: Engine,
    *,
    expected_revision: str,
    tenant_id: str,
    known: frozenset[str] | None,
) -> SchemaState:
    """One read of everything ``materialize`` needs to already be true.

    ``known`` is this image's revision set when it has one. It exists so a
    database that is *ahead* satisfies the fence: on a rollback the release job
    correctly applies nothing and leaves a newer revision behind, and a fence
    that demanded exact equality would then hold the rollback open until it
    timed out. Without it the fence is stricter — it fails loudly rather than
    passing wrongly, but it fails a rollback, which is why the message says so.
    Both deployed images ship a script directory, so ``known`` is ``None`` only
    for a caller that deliberately withheld it.
    """
    revisions = database_revisions(engine)
    unknown = tuple(rev for rev in revisions if known is not None and rev not in known)
    if not revisions:
        revision_ok = False
    elif expected_revision in revisions:
        revision_ok = True
    else:
        revision_ok = bool(unknown)

    inspector = inspect(engine)
    missing = tuple(
        table
        for table in FEATURE_STORE_TABLES
        if not inspector.has_table(table, schema=FEATURE_STORE_SCHEMA)
    )

    has_ratings = False
    if inspector.has_table("ratings"):
        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT 1 FROM ratings WHERE tenant_id = :tenant_id LIMIT 1"),
                {"tenant_id": tenant_id},
            ).first()
        has_ratings = row is not None

    return SchemaState(
        revisions=revisions,
        revision_ok=revision_ok,
        missing_feature_tables=missing,
        tenant_has_ratings=has_ratings,
        unknown_revisions=unknown,
    )


def wait_for_schema(
    engine: Engine,
    *,
    expected_revision: str,
    tenant_id: str,
    timeout_seconds: float,
    known: frozenset[str] | None = None,
    poll_seconds: float = _FENCE_POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Block until the release job's schema is in place, or fail the deploy.

    This is the honest replacement for Compose's
    ``depends_on: service_completed_successfully``, which a PaaS has no
    equivalent for. A deadline of 0 still performs one check: the point is that
    the preconditions are never skipped, not that the step always waits.
    """
    deadline = monotonic() + max(timeout_seconds, 0.0)
    while True:
        try:
            state = inspect_schema_state(
                engine,
                expected_revision=expected_revision,
                tenant_id=tenant_id,
                known=known,
            )
        except OperationalError as exc:
            # A database that is not answering yet is the ordinary first-deploy
            # case, not a reason to abort: this fence exists to wait. A
            # permanent misconfiguration lands here too and is reported when the
            # deadline passes, with the driver's own message attached. A refused
            # *privilege* is a different thing and is not retried -- it raises
            # out of database_revisions and ends the step immediately.
            reasons = [f"the database is not answering yet: {exc.orig}"]
        else:
            if state.satisfied:
                satisfaction = state.satisfaction(expected_revision=expected_revision)
                # Logged, not just returned: on the rollback path this line is
                # the difference between "the pre-deploy did nothing because the
                # database is newer" and a silent pass an operator has to infer.
                logger.info("Release schema fence satisfied: %s", satisfaction)
                return {
                    "expected_revision": expected_revision,
                    "database_revisions": list(state.revisions),
                    "ahead_revisions": list(state.unknown_revisions),
                    "tenant_id": tenant_id,
                    "reason": satisfaction,
                }
            reasons = state.unmet(expected_revision=expected_revision, tenant_id=tenant_id)
        if monotonic() >= deadline:
            hint = (
                ""
                if known is not None
                else (
                    " This image cannot enumerate its own migrations, so a database that is "
                    "legitimately ahead (a rollback) also lands here; build the image with "
                    "alembic.ini + alembic/, or pass --expected-revision for this run."
                )
            )
            raise SchemaFenceTimeoutError(
                f"the schema this release depends on did not arrive within "
                f"{timeout_seconds:g}s: {'; '.join(reasons)}.{hint}"
            )
        logger.info("Waiting for the release schema: %s", "; ".join(reasons))
        sleep(poll_seconds)


def _describe_feast_registry(repo_path: Path) -> dict[str, Any] | None:
    """The baked registry's semantic description, or None when there is none."""
    from src.features.registry_check import RegistryCheckError, describe_registry

    try:
        return describe_registry(repo_path)
    except RegistryCheckError as exc:
        logger.warning("No Feast registry to compare against yet: %s", exc)
        return None


def run_materialize(
    settings: Settings,
    *,
    wait_seconds: float,
    expected_revision: str | None = None,
    persona_user_id: int = DEFAULT_PERSONA_USER_ID,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """Publish the online feature store, then prove what was published.

    Ordering matters twice over. The fence runs before the materialize because
    building features against a half-migrated database is how a deploy produces
    a plausible-looking but wrong online store; the registry and online-read
    assertions run after, because they are statements about what this step
    actually did rather than about what it intended to do.
    """
    from src.features.materialize import materialize
    from src.features.online import create_feature_store, get_online_user_features
    from src.features.registry_check import (
        describe_declared_definitions,
        registry_differences,
    )

    resolved_tenant = tenant_id or settings.model_tenant_id
    revision = resolve_expected_revision(expected_revision)
    try:
        known: frozenset[str] | None = known_revisions()
    except SchemaToolingUnavailableError as exc:
        # Both deployed images carry the script directory, so this is a build
        # regression rather than an expected mode. The fence still runs, and is
        # stricter without it: it cannot recognise a database that is ahead, so
        # a rollback would wait out its deadline instead of passing.
        logger.warning(
            "This image cannot enumerate its own migrations (%s); the fence cannot "
            "recognise a database that is ahead of this release.",
            exc,
        )
        known = None

    engine = create_engine(settings.admin_user_database_url, future=True)
    try:
        fence = wait_for_schema(
            engine,
            expected_revision=revision,
            tenant_id=resolved_tenant,
            timeout_seconds=wait_seconds,
            known=known,
        )
    finally:
        engine.dispose()

    repo_path = Path(settings.feast_repo_path)
    # Captured before the apply overwrites it: this container is thrown away,
    # so the registry the sidecars will actually serve from is the baked one.
    baked = _describe_feast_registry(repo_path)

    counts = materialize(settings)

    applied = _describe_feast_registry(repo_path)
    if applied is None:
        raise ReleaseError(
            f"materialize applied the feature definitions but wrote no registry under "
            f"{repo_path}; the sidecars would load definitions nobody applied"
        )
    declared = describe_declared_definitions(project=str(applied["project"]))
    drift = registry_differences(applied, declared)
    if drift:
        raise ReleaseError(
            "the registry this release applied does not match the feature definitions in "
            f"this image; differing paths: {', '.join(drift)}"
        )
    baked_drift = registry_differences(applied, baked) if baked is not None else []
    if baked_drift:
        raise ReleaseError(
            "the registry baked into this image differs from the one this release applied, "
            "so the sidecars would serve definitions this materialize did not write; "
            f"differing paths: {', '.join(baked_drift)}. Rebuild the features image."
        )

    store = create_feature_store(settings)
    values = get_online_user_features(store, tenant_id=resolved_tenant, user_id=persona_user_id)
    populated = {
        name: value for name, value in values.items() if value is not None and float(value) != 0.0
    }
    if not populated:
        raise ReleaseError(
            f"the online store answered for tenant {resolved_tenant!r} user {persona_user_id} "
            f"with an all-default feature frame ({values!r}). Feast returns 0.0 for a missing "
            "feature rather than raising, so this is what an empty Redis looks like from the "
            "ranker's side."
        )
    return {
        "step": "materialize",
        "fence": fence,
        "rows": counts,
        "registry": {
            "project": applied["project"],
            "feature_views": sorted(applied["feature_views"]),
            "baked_registry": "compared" if baked is not None else "absent",
        },
        "online_read": {"user_id": persona_user_id, "features": values},
    }


# --------------------------------------------------------------------------
# all
# --------------------------------------------------------------------------


def run_schema(settings: Settings, *, config_path: Path = DEFAULT_ALEMBIC_INI) -> dict[str, Any]:
    """``schema`` against the migrator DSN."""
    engine = create_engine(settings.database_url, future=True)
    try:
        outcome = apply_schema(engine, config_path=config_path)
    finally:
        engine.dispose()
    return {
        "step": "schema",
        "applied": outcome.applied,
        "revision": outcome.revision,
        "reason": outcome.reason,
    }


def run_all(
    settings: Settings,
    *,
    realm: str | None = None,
    config_path: Path = DEFAULT_ALEMBIC_INI,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """preflight, then schema, then seed. The release job's whole command."""
    preflight = run_preflight(settings, realm=realm, timeout=timeout)
    schema = run_schema(settings, config_path=config_path)
    engine = create_engine(settings.database_url, future=True)
    try:
        seed = run_seed(engine)
    finally:
        engine.dispose()
    return {"step": "all", "preflight": preflight, "schema": schema, "seed": seed}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.release.bootstrap",
        description="Bring a deployed environment up to the state the services expect.",
    )
    parser.add_argument(
        "subcommand",
        choices=("preflight", "schema", "seed", "materialize", "all"),
    )
    parser.add_argument(
        "--realm",
        default=None,
        help="Keycloak realm for the issuer-equality check (default: MODEL_TENANT_ID).",
    )
    parser.add_argument(
        "--wait-for-schema",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help=(
            "materialize: how long to wait for the release job's schema. 0 still checks "
            "once and fails if the schema is not in place (default: %(default)s)."
        ),
    )
    parser.add_argument(
        "--expected-revision",
        default=None,
        help=(
            "materialize: the Alembic head this release expects. Defaults to this "
            "image's own script directory, which is the answer in every deployed image."
        ),
    )
    parser.add_argument(
        "--persona-user-id",
        type=int,
        default=DEFAULT_PERSONA_USER_ID,
        help="materialize: the persona whose online features must read back non-default.",
    )
    parser.add_argument(
        "--tenant-id",
        default=None,
        help="materialize: tenant to fence and read on (default: MODEL_TENANT_ID).",
    )
    parser.add_argument(
        "--alembic-config",
        type=Path,
        default=DEFAULT_ALEMBIC_INI,
        help="Path to alembic.ini (default: %(default)s).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=_DEFAULT_TIMEOUT_SECONDS,
        help="Per-dependency timeout for the preflight probes (default: %(default)s).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    args = build_parser().parse_args(argv)
    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001 - pydantic and the guards both land here
        print(
            f"[release] Settings() refused to construct: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    try:
        summary = _dispatch(args, settings)
    except ReleaseError as exc:
        print(f"[release] {args.subcommand} failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    if args.subcommand == "all":
        # The deploy gate greps for this line rather than trusting the
        # deployment state, and only the whole sequence earns it: a preflight
        # that printed it would let a partial release look like a complete one.
        print(f"{RELEASE_BOOTSTRAP_SENTINEL} {summary['schema']['revision']}")
    return 0


def _dispatch(args: argparse.Namespace, settings: Settings) -> dict[str, Any]:
    if args.subcommand == "preflight":
        return run_preflight(settings, realm=args.realm, timeout=args.timeout)
    if args.subcommand == "schema":
        return run_schema(settings, config_path=args.alembic_config)
    if args.subcommand == "seed":
        engine = create_engine(settings.database_url, future=True)
        try:
            return run_seed(engine)
        finally:
            engine.dispose()
    if args.subcommand == "materialize":
        return run_materialize(
            settings,
            wait_seconds=args.wait_for_schema,
            expected_revision=args.expected_revision,
            persona_user_id=args.persona_user_id,
            tenant_id=args.tenant_id,
        )
    return run_all(
        settings,
        realm=args.realm,
        config_path=args.alembic_config,
        timeout=args.timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
