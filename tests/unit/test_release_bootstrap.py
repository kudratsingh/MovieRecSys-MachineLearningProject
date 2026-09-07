"""Unit coverage for the release bootstrap: its no-ops, its fences, its dispatch.

The headline case is the DB-ahead no-op. A pre-deploy command runs on every
deploy, rollbacks included, so an older image's ``alembic upgrade head`` against
a database carrying a newer revision raises "Can't locate revision" and the
rollback that was supposed to end an incident fails instead. The test that
matters is not that the step reports something sensible — it is that it applies
*nothing* and exits 0.

The schema tests run against in-memory SQLite with a ``StaticPool`` so an
``ATTACH``ed ``feature_store`` schema survives between connections. That is
enough for what is being asserted here: which revisions the database reports,
which tables exist, and whether the migration step was invoked at all. The
Postgres-specific halves (RLS, the pgBouncer console, the real migrations) are
proven by ``tests/tenant_isolation/`` and the rehearsal, not by unit tests.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import StaticPool

from src.config import Settings
from src.release import RELEASE_BOOTSTRAP_SENTINEL, bootstrap

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
UNKNOWN_REVISION = "9999_a_revision_this_image_has_never_heard_of"


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both serving images default to ENVIRONMENT=production and refuse the dev
    credential defaults there, so a test that means to read the in-code defaults
    has to say so explicitly."""
    for name in (
        "ENVIRONMENT",
        "MODEL_SERVER_AUTH_TOKEN",
        "PGBOUNCER_ADMIN_USER",
        "PGBOUNCER_ADMIN_PASSWORD",
        "REDIS_CONNECTION_STRING",
    ):
        monkeypatch.delenv(name, raising=False)


def _dev_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, environment="dev", **overrides)


def _production_settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        environment="production",
        model_server_auth_token="a-generated-model-server-token",
        pgbouncer_admin_password="a-generated-pgbouncer-password",
        **overrides,
    )


def _sqlite_engine() -> Engine:
    return create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        future=True,
    )


def _database_at(revision: str | None, *, ratings_tenant: str | None = None) -> Engine:
    """A database reporting ``revision``, optionally with a seeded tenant."""
    engine = _sqlite_engine()
    with engine.begin() as connection:
        if revision is not None:
            connection.execute(text("CREATE TABLE alembic_version (version_num TEXT)"))
            connection.execute(
                text("INSERT INTO alembic_version VALUES (:revision)"), {"revision": revision}
            )
        if ratings_tenant is not None:
            connection.execute(text("CREATE TABLE ratings (tenant_id TEXT)"))
            connection.execute(
                text("INSERT INTO ratings VALUES (:tenant)"), {"tenant": ratings_tenant}
            )
    return engine


def _attach_feature_store(engine: Engine, tables: tuple[str, ...]) -> None:
    with engine.begin() as connection:
        connection.execute(text("ATTACH DATABASE ':memory:' AS feature_store"))
        for table in tables:
            connection.execute(text(f"CREATE TABLE feature_store.{table} (a INTEGER)"))


class _PrepareSpy:
    """Stand-in for ``prepare_demo_database``; records that it was called."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, engine: Engine, **kwargs: Any) -> None:
        self.calls.append({"engine": engine, **kwargs})


# --------------------------------------------------------------------------
# the DB-ahead no-op
# --------------------------------------------------------------------------


def test_schema_applies_nothing_when_the_database_revision_is_unknown() -> None:
    engine = _database_at(UNKNOWN_REVISION)
    prepare = _PrepareSpy()

    outcome = bootstrap.apply_schema(engine, prepare=prepare)

    assert outcome.applied is False
    assert prepare.calls == [], "a rollback must not run this image's migrations"
    assert outcome.revision == UNKNOWN_REVISION
    assert "ahead of this release" in outcome.reason


def test_schema_no_op_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The rollback path is only safe if the pre-deploy command *succeeds*."""
    engine = _database_at(UNKNOWN_REVISION)
    prepare = _PrepareSpy()
    monkeypatch.setattr("src.data.demo_setup.prepare_demo_database", prepare)
    monkeypatch.setattr(bootstrap, "Settings", _dev_settings)
    monkeypatch.setattr(bootstrap, "create_engine", lambda *args, **kwargs: engine)

    assert bootstrap.main(["schema"]) == 0
    assert prepare.calls == []
    assert '"applied": false' in capsys.readouterr().out


def test_schema_applies_on_a_fresh_database() -> None:
    engine = _database_at(None)
    prepare = _PrepareSpy()

    outcome = bootstrap.apply_schema(engine, prepare=prepare)

    assert outcome.applied is True
    assert len(prepare.calls) == 1
    assert outcome.revision == bootstrap.head_revision()


def test_schema_applies_when_the_database_is_behind_this_image() -> None:
    engine = _database_at("0001")
    prepare = _PrepareSpy()

    outcome = bootstrap.apply_schema(engine, prepare=prepare)

    assert outcome.applied is True
    assert len(prepare.calls) == 1


def test_prepare_receives_a_cwd_independent_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    """``alembic.ini`` resolves ``script_location`` against cwd; a job's cwd is
    whatever the platform's start command left it at."""
    engine = _database_at(None)
    prepare = _PrepareSpy()
    monkeypatch.chdir(Path(os.devnull).parent)

    bootstrap.apply_schema(engine, prepare=prepare)

    upgrade = prepare.calls[0]["upgrade"]
    applied: list[str] = []
    monkeypatch.setattr(
        "alembic.command.upgrade", lambda config, revision: applied.append(revision)
    )
    upgrade(None, "head")
    assert applied == ["head"]


# --------------------------------------------------------------------------
# revision resolution
# --------------------------------------------------------------------------


def test_head_and_known_revisions_come_from_this_tree() -> None:
    known = bootstrap.known_revisions()
    assert "0001" in known
    assert bootstrap.head_revision() in known
    assert UNKNOWN_REVISION not in known


def test_head_revision_does_not_depend_on_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    assert bootstrap.head_revision() == bootstrap.head_revision(REPOSITORY_ROOT / "alembic.ini")


def test_expected_revision_is_this_image_s_own_head_unless_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No environment variable feeds this any more.

    ``RELEASE_EXPECTED_REVISION`` existed while the features image shipped no
    Alembic. It ships one now, so the image answers for itself and the only
    override left is the flag — a hand-maintained literal in a variable panel
    could only ever be right until the next migration."""
    monkeypatch.setenv("RELEASE_EXPECTED_REVISION", "0001_a_stale_hand_written_literal")
    assert bootstrap.resolve_expected_revision("0007") == "0007"
    assert bootstrap.resolve_expected_revision(None) == bootstrap.head_revision()


def test_an_image_without_a_script_directory_says_so_rather_than_tracing(
    tmp_path: Path,
) -> None:
    """An image built without alembic/ is a build regression, and its fence
    needs the message rather than a ModuleNotFoundError from a pre-deploy."""
    with pytest.raises(bootstrap.SchemaToolingUnavailableError, match="Alembic config"):
        bootstrap.head_revision(tmp_path / "alembic.ini")


def test_expected_revision_names_every_remedy_when_the_image_has_no_alembic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(config_path: Path = bootstrap.DEFAULT_ALEMBIC_INI) -> str:
        raise bootstrap.SchemaToolingUnavailableError("no script directory")

    monkeypatch.setattr(bootstrap, "head_revision", unavailable)
    with pytest.raises(bootstrap.ReleaseError) as error:
        bootstrap.resolve_expected_revision(None)
    message = str(error.value)
    assert "--expected-revision" in message
    assert "infra/features/Dockerfile" in message
    assert "infra/features/requirements.txt" in message


# --------------------------------------------------------------------------
# the materialize fence
# --------------------------------------------------------------------------


def _fenced_engine(revision: str) -> Engine:
    engine = _database_at(revision, ratings_tenant="demo")
    _attach_feature_store(engine, bootstrap.FEATURE_STORE_TABLES)
    return engine


def test_the_fence_is_satisfied_by_this_image_s_own_head() -> None:
    head = bootstrap.head_revision()
    state = bootstrap.inspect_schema_state(
        _fenced_engine(head), expected_revision=head, tenant_id="demo", known=None
    )
    assert state.satisfied is True


def test_the_fence_treats_a_database_ahead_as_satisfied() -> None:
    """Rollbacks leave a newer schema behind; a fence demanding equality would
    hold the rollback open until it timed out."""
    state = bootstrap.inspect_schema_state(
        _fenced_engine(UNKNOWN_REVISION),
        expected_revision=bootstrap.head_revision(),
        tenant_id="demo",
        known=bootstrap.known_revisions(),
    )
    assert state.satisfied is True


def test_the_fence_cannot_recognise_a_database_ahead_without_the_script_set() -> None:
    state = bootstrap.inspect_schema_state(
        _fenced_engine(UNKNOWN_REVISION),
        expected_revision=bootstrap.head_revision(),
        tenant_id="demo",
        known=None,
    )
    assert state.satisfied is False


def test_the_fence_names_every_precondition_it_is_still_waiting_on() -> None:
    engine = _database_at(None)
    state = bootstrap.inspect_schema_state(
        engine, expected_revision="0012", tenant_id="demo", known=None
    )
    reasons = " ".join(state.unmet(expected_revision="0012", tenant_id="demo"))
    assert "alembic_version is none" in reasons
    assert "user_features" in reasons
    assert "no ratings" in reasons


def test_the_fence_fails_the_deploy_when_the_deadline_passes() -> None:
    engine = _database_at(None)
    clock = iter([0.0, 0.0, 5.0, 5.0])
    with pytest.raises(bootstrap.SchemaFenceTimeoutError) as error:
        bootstrap.wait_for_schema(
            engine,
            expected_revision="0012",
            tenant_id="demo",
            timeout_seconds=1.0,
            poll_seconds=0.0,
            sleep=lambda _seconds: None,
            monotonic=lambda: next(clock),
        )
    assert "did not arrive within" in str(error.value)


def test_the_fence_waits_out_a_database_that_is_not_answering_yet() -> None:
    """A first deploy races Postgres coming up; the fence exists to wait."""
    engine = create_engine("postgresql+psycopg2://nobody@nowhere.invalid:5432/none", future=True)
    clock = iter([0.0, 0.0, 900.0])
    with pytest.raises(bootstrap.SchemaFenceTimeoutError) as error:
        bootstrap.wait_for_schema(
            engine,
            expected_revision="0012",
            tenant_id="demo",
            timeout_seconds=300.0,
            poll_seconds=0.0,
            sleep=lambda _seconds: None,
            monotonic=lambda: next(clock),
        )
    assert "not answering yet" in str(error.value)


def test_the_fence_returns_once_the_release_job_has_landed() -> None:
    head = bootstrap.head_revision()
    engine = _fenced_engine(head)
    summary = bootstrap.wait_for_schema(
        engine,
        expected_revision=head,
        tenant_id="demo",
        timeout_seconds=0.0,
        known=bootstrap.known_revisions(),
        sleep=lambda _seconds: None,
    )
    assert summary["database_revisions"] == [head]
    assert summary["ahead_revisions"] == []
    assert head in summary["reason"]


def test_the_fence_passes_a_rollback_immediately_and_says_why(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The other half of the same fence: a database ahead of this image.

    This is the deploy that would otherwise wait out its whole 300 s and fail —
    the release job correctly applied nothing, so the revision it left behind is
    newer than anything this image knows. It has to pass, and it has to say so
    in the log, because a silent pass and a hung pre-deploy look identical from
    the outside until the timeout."""
    engine = _fenced_engine(UNKNOWN_REVISION)
    with caplog.at_level("INFO", logger="release.bootstrap"):
        summary = bootstrap.wait_for_schema(
            engine,
            expected_revision=bootstrap.head_revision(),
            tenant_id="demo",
            timeout_seconds=0.0,
            known=bootstrap.known_revisions(),
            sleep=lambda _seconds: None,
        )
    assert summary["ahead_revisions"] == [UNKNOWN_REVISION]
    assert "ahead of this release" in summary["reason"]
    assert "ahead of this release" in caplog.text


def test_the_features_image_can_answer_both_halves_of_the_fence() -> None:
    """The fence's two branches both need this image's own revision set.

    ``known_revisions()`` reading the tree is what makes "unknown to this image"
    mean "newer than this release" rather than "this image cannot tell". The
    features image is built with alembic.ini + alembic/ for exactly this call;
    a build that dropped them would fail here rather than in a pre-deploy."""
    known = bootstrap.known_revisions()
    assert bootstrap.head_revision() in known
    assert UNKNOWN_REVISION not in known

    at_head = bootstrap.inspect_schema_state(
        _fenced_engine(bootstrap.head_revision()),
        expected_revision=bootstrap.head_revision(),
        tenant_id="demo",
        known=known,
    )
    ahead = bootstrap.inspect_schema_state(
        _fenced_engine(UNKNOWN_REVISION),
        expected_revision=bootstrap.head_revision(),
        tenant_id="demo",
        known=known,
    )
    assert (at_head.satisfied, at_head.unknown_revisions) == (True, ())
    assert (ahead.satisfied, ahead.unknown_revisions) == (True, (UNKNOWN_REVISION,))


# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------


def test_preflight_refuses_a_non_production_environment(clean_env: None) -> None:
    with pytest.raises(bootstrap.PreflightError) as error:
        bootstrap.check_environment_is_production(_dev_settings())
    assert "ENVIRONMENT=production" in str(error.value)


def test_preflight_accepts_production(clean_env: None) -> None:
    assert bootstrap.check_environment_is_production(_production_settings()) == "production"


def test_authorized_parties_must_be_a_non_empty_json_list() -> None:
    with pytest.raises(bootstrap.PreflightError, match="empty"):
        bootstrap.check_authorized_parties(SimpleNamespace(keycloak_authorized_parties=()))


def test_authorized_parties_rejects_csv_smuggled_into_one_entry() -> None:
    """Valid JSON, constructs fine, and rejects every real token."""
    settings = SimpleNamespace(
        keycloak_authorized_parties=("movielens-api,movielens-web",),
    )
    with pytest.raises(bootstrap.PreflightError, match="CSV"):
        bootstrap.check_authorized_parties(settings)


def test_authorized_parties_accepts_the_deployment_shape(clean_env: None) -> None:
    settings = _production_settings(
        keycloak_authorized_parties=("movielens-api", "movielens-web", "movielens-verify")
    )
    assert bootstrap.check_authorized_parties(settings) == [
        "movielens-api",
        "movielens-web",
        "movielens-verify",
    ]


@pytest.mark.parametrize(
    ("connection_string", "host", "port", "options"),
    [
        ("redis:6379", "redis", 6379, {}),
        (
            "redis.internal:6379,password=s3cret,ssl=true",
            "redis.internal",
            6379,
            {"password": "s3cret", "ssl": "true"},
        ),
    ],
)
def test_redis_connection_strings_are_feast_shaped(
    connection_string: str, host: str, port: int, options: dict[str, str]
) -> None:
    endpoint, parsed = bootstrap.parse_redis_connection_string(connection_string)
    assert (endpoint.host, endpoint.port) == (host, port)
    assert parsed == options


@pytest.mark.parametrize("connection_string", ["", "redis", "redis:6379,password"])
def test_malformed_redis_connection_strings_are_refused(connection_string: str) -> None:
    with pytest.raises(bootstrap.PreflightError):
        bootstrap.parse_redis_connection_string(connection_string)


def test_the_resp_reader_understands_config_get() -> None:
    reply = io.BytesIO(b"*2\r\n$16\r\nmaxmemory-policy\r\n$10\r\nnoeviction\r\n")
    assert bootstrap._read_resp(reply) == ["maxmemory-policy", "noeviction"]


def test_the_resp_reader_surfaces_a_redis_error() -> None:
    with pytest.raises(bootstrap.PreflightError, match="WRONGPASS"):
        bootstrap._read_resp(io.BytesIO(b"-WRONGPASS invalid password\r\n"))


def test_resp_commands_are_encoded_as_arrays() -> None:
    assert bootstrap._resp_command("AUTH", "hunter2") == b"*2\r\n$4\r\nAUTH\r\n$7\r\nhunter2\r\n"


def test_an_evicting_redis_fails_preflight(
    monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    monkeypatch.setattr(bootstrap, "redis_config_get", lambda *a, **k: "allkeys-lru")
    with pytest.raises(bootstrap.PreflightError, match="noeviction"):
        bootstrap.check_redis_eviction_policy(_production_settings())


def test_noeviction_passes_preflight(monkeypatch: pytest.MonkeyPatch, clean_env: None) -> None:
    monkeypatch.setattr(bootstrap, "redis_config_get", lambda *a, **k: "noeviction")
    assert bootstrap.check_redis_eviction_policy(_production_settings()) == "noeviction"


def test_issuer_equality_accepts_the_document_that_names_this_deployment() -> None:
    issuer = bootstrap.check_issuer_equality(
        public_base_url="https://auth.example.com/",
        realm="demo",
        fetch=lambda url: {"issuer": "https://auth.example.com/realms/demo"},
    )
    assert issuer == "https://auth.example.com/realms/demo"


def test_issuer_equality_refuses_an_internal_issuer() -> None:
    """The likeliest first-deploy failure: Keycloak reports the address it was
    reached on rather than the public one every token has to carry."""
    with pytest.raises(bootstrap.PreflightError, match="KEYCLOAK_PUBLIC_BASE_URL"):
        bootstrap.check_issuer_equality(
            public_base_url="https://auth.example.com",
            realm="demo",
            fetch=lambda url: {"issuer": "http://keycloak:8080/realms/demo"},
        )


# --------------------------------------------------------------------------
# seed
# --------------------------------------------------------------------------


def test_seed_reports_what_it_wrote(monkeypatch: pytest.MonkeyPatch) -> None:
    result = SimpleNamespace(
        tenant_id="demo",
        persona_count=4,
        persona_rating_count=27,
        background_rating_count=480,
        catalog_movie_count=62423,
        reviewed_movie_count=120,
        recommendable_movie_count=120,
        poster_movie_count=24,
    )
    monkeypatch.setattr("synthetic.personas.seed.seed_demo_personas", lambda engine: result)

    summary = bootstrap.run_seed(_database_at(None))

    assert summary["tenant_id"] == "demo"
    assert summary["personas"] == 4
    assert summary["poster_movies"] == 24
    # Two numbers, because they are now two orders of magnitude apart: a release
    # log that printed only one would make a catalog with no artwork behind it
    # look like a fully enriched one.
    assert summary["catalog_movies"] == 62423
    assert summary["reviewed_movies"] == 120


# --------------------------------------------------------------------------
# the CLI and the image entrypoint
# --------------------------------------------------------------------------


def test_only_the_full_sequence_prints_the_deploy_sentinel(
    monkeypatch: pytest.MonkeyPatch, clean_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """A preflight that printed it would let a partial release look complete."""
    monkeypatch.setattr(bootstrap, "Settings", _dev_settings)
    monkeypatch.setattr(bootstrap, "run_preflight", lambda *a, **k: {"step": "preflight"})
    monkeypatch.setattr(
        bootstrap,
        "run_schema",
        lambda *a, **k: {"step": "schema", "applied": True, "revision": "0012"},
    )
    monkeypatch.setattr(bootstrap, "run_seed", lambda engine: {"step": "seed"})
    monkeypatch.setattr(bootstrap, "create_engine", lambda *a, **k: _database_at(None))

    assert bootstrap.main(["all"]) == 0
    assert f"{RELEASE_BOOTSTRAP_SENTINEL} 0012" in capsys.readouterr().out.splitlines()[-1]

    monkeypatch.setattr(bootstrap, "run_preflight", lambda *a, **k: {"step": "preflight"})
    assert bootstrap.main(["preflight"]) == 0
    assert RELEASE_BOOTSTRAP_SENTINEL not in capsys.readouterr().out


def test_a_failing_step_exits_non_zero(monkeypatch: pytest.MonkeyPatch, clean_env: None) -> None:
    def refuse(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise bootstrap.PreflightError("Redis maxmemory-policy is 'allkeys-lru'")

    monkeypatch.setattr(bootstrap, "Settings", _dev_settings)
    monkeypatch.setattr(bootstrap, "run_preflight", refuse)
    assert bootstrap.main(["preflight"]) == 1


@pytest.fixture
def shimmed_path(tmp_path: Path) -> str:
    """A PATH where ``uvicorn`` and ``python`` just echo how they were called."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("uvicorn", "python"):
        shim = bin_dir / name
        shim.write_text(f'#!/bin/sh\necho "{name} $*"\n')
        shim.chmod(0o755)
    return f"{bin_dir}:{os.environ['PATH']}"


def _entrypoint(shimmed_path: str, *argv: str, **env: str) -> str:
    completed = subprocess.run(
        ["sh", str(REPOSITORY_ROOT / "infra" / "api" / "entrypoint.sh"), *argv],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": shimmed_path, **env},
    )
    return completed.stdout.strip()


def test_entrypoint_serve_reads_its_worker_count_and_port(shimmed_path: str) -> None:
    output = _entrypoint(shimmed_path, "serve", API_WORKERS="4", PORT="8000")
    assert "src.serving.app:app" in output
    assert "--workers 4" in output
    assert "--port 8000" in output
    assert "--no-access-log" not in output


def test_entrypoint_serve_defaults_to_four_workers(shimmed_path: str) -> None:
    assert "--workers 4" in _entrypoint(shimmed_path, "serve")


def test_entrypoint_dispatches_the_release_modes(shimmed_path: str) -> None:
    assert _entrypoint(shimmed_path, "bootstrap", "all") == "python -m src.release.bootstrap all"
    assert _entrypoint(shimmed_path, "verify", "--all") == "python -m src.release.verify --all"


def test_entrypoint_passes_an_unrecognised_command_through(shimmed_path: str) -> None:
    """Compose already passes explicit commands to this image, and a platform
    that appends its start command to the ENTRYPOINT lands here too."""
    assert _entrypoint(shimmed_path, "python", "-m", "src.data.demo_setup") == (
        "python -m src.data.demo_setup"
    )


def test_the_api_image_dispatches_through_the_entrypoint() -> None:
    dockerfile = (REPOSITORY_ROOT / "infra" / "api" / "Dockerfile").read_text()
    assert "COPY infra/api/entrypoint.sh /usr/local/bin/entrypoint.sh" in dockerfile
    assert 'ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]' in dockerfile
    assert 'CMD ["serve"]' in dockerfile


def test_the_release_modules_import_nothing_the_slim_api_image_lacks() -> None:
    """``src.release`` is imported by the API image, which ships no Feast,
    pandas, LightGBM or numpy — the subcommands that need them import lazily."""
    probe = (
        "import sys\n"
        "forbidden = ('numpy', 'pandas', 'lightgbm', 'feast', 'faiss', 'torch')\n"
        "import src.release.bootstrap\n"
        "import src.release.verify\n"
        "leaked = sorted(name for name in forbidden if name in sys.modules)\n"
        "raise SystemExit(f'heavy modules reached the release job: {leaked}' if leaked else 0)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=REPOSITORY_ROOT,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
