"""Idempotently seed the tenant-scoped portfolio demo fixtures."""

from __future__ import annotations

import csv
import gzip
import io
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import JSON, Connection, Engine, TextClause, bindparam, create_engine, text

from src.config import Settings

_FIXTURE_DIR = Path(__file__).parent
_BASE_TIMESTAMP = 1_700_000_000
_CATALOG_SNAPSHOT_AT = "2026-08-21T00:00:00+00:00"
# The MovieLens release the snapshot below was projected from, and the date it
# was taken. Both are literals for the same reason `_CATALOG_SNAPSHOT_AT` is:
# `source_updated_at` must not move when a re-seed runs on a different day.
_MOVIELENS_SNAPSHOT_AT = "2026-09-05T00:00:00+00:00"

# Migration 0011's `ck_catalog_release_year` accepts 1878..2100. MovieLens ships
# exactly one title below that floor (148054, "Passage de Venus (1874)"), so an
# out-of-range year is dropped rather than the row — the alternative is the
# whole catalog insert failing over one 19th-century short.
_CATALOG_MIN_YEAR = 1878
_CATALOG_MAX_YEAR = 2100

# Postgres caps a statement at 65535 bind parameters and SQLAlchemy's
# executemany batches by rows, not by parameters. Five thousand rows keeps every
# statement here inside that ceiling without turning 62k inserts into 62k round
# trips.
_INSERT_CHUNK_SIZE = 5_000


@dataclass(frozen=True)
class Persona:
    slug: str
    display_name: str
    description: str
    user_id: int
    history: tuple[int, ...]


@dataclass(frozen=True)
class CatalogMovie:
    movie_id: int
    title: str
    genres: str
    tmdb_id: str | None
    release_year: int | None
    poster_url: str | None
    overview: str | None
    # The offline detail payload (``synthetic/personas/enrich_details.py``):
    # trailer, tagline, runtime, release date, backdrop, crowd score,
    # directors, billed cast. Absent for a title the enrichment has not
    # reached, which the detail page renders as the layout it had before.
    details: dict[str, Any] | None


@dataclass(frozen=True)
class SeedResult:
    tenant_id: str
    persona_count: int
    persona_rating_count: int
    background_rating_count: int
    # Every title the seed leaves visible in `movie_catalog_metadata` — the full
    # MovieLens catalog — against the reviewed subset that carries artwork and
    # editorial copy. They were one number while the demo database held 120
    # movies; they are 62,423 and 120 now, and conflating them is exactly how a
    # sparse Browse grid would read as a healthy one.
    catalog_movie_count: int
    reviewed_movie_count: int
    recommendable_movie_count: int
    poster_movie_count: int
    detail_movie_count: int


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fixture_file:
        value = json.load(fixture_file)
    if not isinstance(value, dict):
        raise ValueError(f"fixture must contain a JSON object: {path}")
    return value


def load_personas(path: Path = _FIXTURE_DIR / "personas.json") -> tuple[str, list[Persona]]:
    payload = _read_json(path)
    tenant_id = str(payload["tenant_id"])
    personas = [
        Persona(
            slug=str(item["slug"]),
            display_name=str(item["display_name"]),
            description=str(item["description"]),
            user_id=int(item["user_id"]),
            history=tuple(int(movie_id) for movie_id in item["history"]),
        )
        for item in payload["personas"]
    ]
    _validate_personas(personas)
    return tenant_id, personas


def _validate_personas(personas: list[Persona]) -> None:
    if len(personas) != 4:
        raise ValueError("the demo fixture must define exactly four personas")
    if len({persona.slug for persona in personas}) != len(personas):
        raise ValueError("persona slugs must be unique")
    if len({persona.user_id for persona in personas}) != len(personas):
        raise ValueError("persona user IDs must be unique")
    for persona in personas:
        if len(set(persona.history)) != len(persona.history):
            raise ValueError(f"persona {persona.slug!r} contains duplicate history entries")


def load_demo_catalog(
    path: Path = _FIXTURE_DIR / "catalog.json",
) -> tuple[list[CatalogMovie], tuple[int, ...]]:
    payload = _read_json(path)
    movies = [
        CatalogMovie(
            movie_id=int(item["movie_id"]),
            title=str(item["title"]),
            genres=str(item["genres"]),
            tmdb_id=(str(item["tmdb_id"]) if item.get("tmdb_id") is not None else None),
            release_year=(
                int(item["release_year"])
                if item.get("release_year") is not None
                else _release_year_from_title(str(item["title"]))
            ),
            poster_url=(str(item["poster_url"]) if item.get("poster_url") is not None else None),
            overview=(str(item["overview"]) if item.get("overview") is not None else None),
            details=(dict(item["details"]) if isinstance(item.get("details"), dict) else None),
        )
        for item in payload["movies"]
    ]
    background_user_ids = tuple(int(user_id) for user_id in payload["background_user_ids"])
    if not movies or not background_user_ids:
        raise ValueError("demo catalog and background user lists must not be empty")
    if len({movie.movie_id for movie in movies}) != len(movies):
        raise ValueError("demo catalog movie IDs must be unique")
    return movies, background_user_ids


def load_movielens_catalog(
    path: Path = _FIXTURE_DIR / "movielens-catalog.csv.gz",
) -> list[CatalogMovie]:
    """Read the full MovieLens catalog snapshot the reviewed fixture sits inside.

    The demo database used to hold only the 120 reviewed titles, which made it
    unable to serve any retriever whose vocabulary is the real catalog: a
    full-data encoder's top-ranked ids simply were not rows, hydration returned
    nothing, and the API fell back to popularity. The snapshot is the floor the
    reviewed fixture is laid on top of — titles, genres and TMDB ids only, no
    artwork and no synopses, because enriching 62k titles is a TMDB budget this
    project has no reason to spend (`enrich_posters.py` covers the 120 a viewer
    actually browses).

    Provenance and the regeneration command are in
    ``synthetic/personas/build_movielens_catalog.py``.
    """
    with gzip.open(path, "rb") as archive:
        reader = csv.DictReader(io.TextIOWrapper(archive, encoding="utf-8", newline=""))
        movies = [
            CatalogMovie(
                movie_id=int(record["movieId"]),
                title=record["title"],
                genres=record["genres"],
                tmdb_id=record["tmdbId"] or None,
                release_year=_catalog_release_year(record["title"]),
                poster_url=None,
                overview=None,
                details=None,
            )
            for record in reader
        ]
    if not movies:
        raise ValueError(f"the MovieLens catalog snapshot is empty: {path}")
    if len({movie.movie_id for movie in movies}) != len(movies):
        raise ValueError("MovieLens catalog snapshot movie IDs must be unique")
    return movies


def seed_demo_personas(
    engine: Engine,
    *,
    persona_path: Path = _FIXTURE_DIR / "personas.json",
    catalog_path: Path = _FIXTURE_DIR / "catalog.json",
    movielens_catalog_path: Path = _FIXTURE_DIR / "movielens-catalog.csv.gz",
) -> SeedResult:
    tenant_id, personas = load_personas(persona_path)
    catalog_movies, background_user_ids = load_demo_catalog(catalog_path)
    movielens_movies = load_movielens_catalog(movielens_catalog_path)
    movie_ids = tuple(movie.movie_id for movie in catalog_movies)

    persona_rows = [
        {
            "tenant_id": tenant_id,
            "user_id": persona.user_id,
            "slug": persona.slug,
            "display_name": persona.display_name,
            "description": persona.description,
            "sort_order": sort_order,
        }
        for sort_order, persona in enumerate(personas)
    ]
    persona_ratings = [
        {
            "tenant_id": tenant_id,
            "user_id": persona.user_id,
            "movie_id": movie_id,
            "rating": 5.0 - (position % 3) * 0.5,
            "timestamp": _BASE_TIMESTAMP + persona_index * 10_000 + position,
        }
        for persona_index, persona in enumerate(personas)
        for position, movie_id in enumerate(persona.history)
    ]
    # A deterministic background cohort gives every title popularity and
    # candidate-graph support without pretending these users are personas.
    # Every visible movie receives four deterministic background interactions.
    # Browse coverage and recommendation eligibility are therefore measured
    # independently but intentionally equal in the reviewed demo fixture.
    background_ratings = [
        {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "movie_id": movie_id,
            "rating": 4.0,
            "timestamp": _BASE_TIMESTAMP - 100_000 + user_index * 1_000 + movie_index,
        }
        for user_index, user_id in enumerate(background_user_ids)
        for movie_index, movie_id in enumerate(movie_ids)
        if (movie_index + user_index) % len(background_user_ids) != 0
    ]

    with engine.begin() as connection:
        # Reviewed first, snapshot second, and the order is the decision: every
        # snapshot write is an ON CONFLICT DO NOTHING, so whatever the reviewed
        # fixture already put there wins. That is not incidental — the reviewed
        # rows differ from raw MovieLens on 31 titles ("The Usual Suspects" for
        # "Usual Suspects, The") and on three genre strings, and those edits are
        # editorial choices a bulk load has no business reverting. It also keeps
        # the ranker fixture's inputs byte-stable: `src/training/demo_artifacts`
        # builds its feature index from an unfiltered read of `movies`, so a
        # genre string changing under it would move `ranker.txt`.
        _ensure_demo_catalog(connection, catalog_movies)
        _ensure_movielens_catalog(connection, movielens_movies)
        _replace_seed_rows(
            connection,
            tenant_id=tenant_id,
            personas=personas,
            background_user_ids=background_user_ids,
            persona_rows=persona_rows,
            rating_rows=persona_ratings + background_ratings,
        )

    catalog_ids = {movie.movie_id for movie in movielens_movies}
    catalog_ids.update(movie.movie_id for movie in catalog_movies)
    return SeedResult(
        tenant_id=tenant_id,
        persona_count=len(persona_rows),
        persona_rating_count=len(persona_ratings),
        background_rating_count=len(background_ratings),
        catalog_movie_count=len(catalog_ids),
        reviewed_movie_count=len(catalog_movies),
        recommendable_movie_count=len({row["movie_id"] for row in background_ratings}),
        poster_movie_count=sum(movie.poster_url is not None for movie in catalog_movies),
        detail_movie_count=sum(movie.details is not None for movie in catalog_movies),
    )


def _ensure_demo_catalog(connection: Connection, movies: list[CatalogMovie]) -> None:
    """Add the snapshot's rows without overwriting a full MovieLens ingest.

    MovieLens-owned rows (``movies``, ``links``) are only ever filled in, never
    replaced: a real ingest is the better source and must win. The catalog read
    model is the other way round — it is fixture-owned, carries no user data,
    and is regenerated offline (``synthetic/personas/enrich_posters.py`` and
    ``enrich_details.py``), so a re-seed is how a refreshed poster, synopsis or
    detail payload reaches an existing database.
    """
    connection.execute(
        text("""
            INSERT INTO movies ("movieId", title, genres)
            VALUES (:movie_id, :title, :genres)
            ON CONFLICT ("movieId") DO NOTHING
            """),
        [
            {"movie_id": movie.movie_id, "title": movie.title, "genres": movie.genres}
            for movie in movies
        ],
    )
    # A TMDB id the fixture has learned since the database was first seeded is
    # worth filling in, but an id that is already there came from an ingest we
    # do not get to second-guess.
    connection.execute(
        text("""
            INSERT INTO links ("movieId", "tmdbId")
            VALUES (:movie_id, :tmdb_id)
            ON CONFLICT ("movieId") DO UPDATE SET "tmdbId" = EXCLUDED."tmdbId"
            WHERE links."tmdbId" IS NULL AND EXCLUDED."tmdbId" IS NOT NULL
            """),
        [{"movie_id": movie.movie_id, "tmdb_id": movie.tmdb_id} for movie in movies],
    )
    # ``details`` is bound as JSON rather than interpolated: the payload is a
    # nested object, and the column is JSONB.
    upsert_catalog = text("""
        INSERT INTO movie_catalog_metadata
            (movie_id, sort_title, release_year, poster_url, overview, details,
             metadata_source, source_status, visible, source_updated_at)
        VALUES
            (:movie_id, :sort_title, :release_year, :poster_url, :overview, :details,
             'reviewed-fixture', :source_status, TRUE, :source_updated_at)
        ON CONFLICT (movie_id) DO UPDATE SET
            sort_title = EXCLUDED.sort_title,
            release_year = EXCLUDED.release_year,
            poster_url = EXCLUDED.poster_url,
            overview = EXCLUDED.overview,
            details = EXCLUDED.details,
            metadata_source = EXCLUDED.metadata_source,
            source_status = EXCLUDED.source_status,
            visible = EXCLUDED.visible,
            source_updated_at = EXCLUDED.source_updated_at
        """).bindparams(bindparam("details", type_=JSON))
    connection.execute(
        upsert_catalog,
        [
            {
                "movie_id": movie.movie_id,
                "sort_title": _sort_title(movie.title),
                "release_year": movie.release_year,
                "poster_url": movie.poster_url,
                "overview": movie.overview,
                "details": movie.details,
                "source_status": ("complete" if movie.poster_url and movie.overview else "partial"),
                "source_updated_at": _CATALOG_SNAPSHOT_AT,
            }
            for movie in movies
        ],
    )


def _ensure_movielens_catalog(connection: Connection, movies: list[CatalogMovie]) -> None:
    """Lay the full MovieLens catalog under whatever is already there.

    Every statement is additive. A row that exists — because the reviewed
    fixture just wrote it, or because a real ingest did — is left exactly as it
    is, which is what makes the seed idempotent and what keeps this from being a
    62,423-row overwrite of curated data on every ``make demo-seed``.

    The metadata rows are honest about what they are: ``metadata_source =
    'movielens'`` and ``source_status = 'unavailable'``, because MovieLens ships
    a title and a genre string and nothing a poster grid wants. They are still
    ``visible``, so Browse renders the whole catalog with placeholders outside
    the reviewed 120 rather than pretending the other 62,303 titles do not
    exist.
    """
    _execute_chunked(
        connection,
        text("""
            INSERT INTO movies ("movieId", title, genres)
            VALUES (:movie_id, :title, :genres)
            ON CONFLICT ("movieId") DO NOTHING
            """),
        [
            {"movie_id": movie.movie_id, "title": movie.title, "genres": movie.genres}
            for movie in movies
        ],
    )
    _execute_chunked(
        connection,
        text("""
            INSERT INTO links ("movieId", "tmdbId")
            VALUES (:movie_id, :tmdb_id)
            ON CONFLICT ("movieId") DO UPDATE SET "tmdbId" = EXCLUDED."tmdbId"
            WHERE links."tmdbId" IS NULL AND EXCLUDED."tmdbId" IS NOT NULL
            """),
        [{"movie_id": movie.movie_id, "tmdb_id": movie.tmdb_id} for movie in movies],
    )
    _execute_chunked(
        connection,
        text("""
            INSERT INTO movie_catalog_metadata
                (movie_id, sort_title, release_year, poster_url, overview, details,
                 metadata_source, source_status, visible, source_updated_at)
            VALUES
                (:movie_id, :sort_title, :release_year, NULL, NULL, NULL,
                 'movielens', 'unavailable', TRUE, :source_updated_at)
            ON CONFLICT (movie_id) DO NOTHING
            """),
        [
            {
                "movie_id": movie.movie_id,
                "sort_title": _sort_title(movie.title),
                "release_year": movie.release_year,
                "source_updated_at": _MOVIELENS_SNAPSHOT_AT,
            }
            for movie in movies
        ],
    )


def _execute_chunked(
    connection: Connection,
    statement: TextClause,
    rows: Sequence[dict[str, Any]],
) -> None:
    for start in range(0, len(rows), _INSERT_CHUNK_SIZE):
        chunk = rows[start : start + _INSERT_CHUNK_SIZE]
        if chunk:
            connection.execute(statement, list(chunk))


def _replace_seed_rows(
    connection: Connection,
    *,
    tenant_id: str,
    personas: list[Persona],
    background_user_ids: tuple[int, ...],
    persona_rows: list[dict[str, object]],
    rating_rows: list[dict[str, object]],
) -> None:
    user_ids = tuple(persona.user_id for persona in personas) + background_user_ids
    delete_ratings = text(
        'DELETE FROM ratings WHERE tenant_id = :tenant_id AND "userId" IN :user_ids'
    ).bindparams(bindparam("user_ids", expanding=True))
    connection.execute(delete_ratings, {"tenant_id": tenant_id, "user_ids": user_ids})
    delete_state = text(
        "DELETE FROM user_movie_state WHERE tenant_id = :tenant_id AND user_id IN :user_ids"
    ).bindparams(bindparam("user_ids", expanding=True))
    connection.execute(delete_state, {"tenant_id": tenant_id, "user_ids": user_ids})
    connection.execute(
        text("DELETE FROM demo_personas WHERE tenant_id = :tenant_id"),
        {"tenant_id": tenant_id},
    )
    connection.execute(
        text("""
            INSERT INTO demo_personas
                (tenant_id, user_id, slug, display_name, description, sort_order, synthetic)
            VALUES
                (:tenant_id, :user_id, :slug, :display_name, :description, :sort_order, TRUE)
            """),
        persona_rows,
    )
    if rating_rows:
        connection.execute(
            text("""
                INSERT INTO ratings (tenant_id, "userId", "movieId", rating, timestamp)
                VALUES (:tenant_id, :user_id, :movie_id, :rating, :timestamp)
                """),
            rating_rows,
        )
        state_rows = [
            {
                **row,
                "watched_at": datetime.fromtimestamp(_fixture_int(row["timestamp"]), UTC),
            }
            for row in rating_rows
        ]
        connection.execute(
            text("""
                INSERT INTO user_movie_state (
                    tenant_id, user_id, movie_id, watched_at, rating,
                    rating_updated_at, state_version, updated_at
                ) VALUES (
                    :tenant_id, :user_id, :movie_id, :watched_at, :rating,
                    :watched_at, 1, :watched_at
                )
                """),
            state_rows,
        )
        insert_seed_event = text("""
            INSERT INTO user_feedback_events (
                tenant_id, event_id, actor_user_id, user_id, movie_id, action,
                old_value, new_value, state_version, outcome, created_at
            ) VALUES (
                :tenant_id, :event_id, 'seed:demo-personas', :user_id, :movie_id,
                'rating_imported', NULL, :new_value, 1, 'backfilled', :created_at
            )
            ON CONFLICT (tenant_id, event_id) DO NOTHING
            """).bindparams(bindparam("new_value", type_=JSON))
        connection.execute(
            insert_seed_event,
            [
                {
                    "tenant_id": str(row["tenant_id"]),
                    "event_id": str(
                        uuid5(
                            NAMESPACE_URL,
                            f"movielens-demo:{row['tenant_id']}:{row['user_id']}:{row['movie_id']}",
                        )
                    ),
                    "user_id": _fixture_int(row["user_id"]),
                    "movie_id": _fixture_int(row["movie_id"]),
                    "new_value": {
                        "rating": _fixture_float(row["rating"]),
                        "watched_at": _fixture_datetime(row["watched_at"]).isoformat(),
                    },
                    "created_at": _fixture_datetime(row["watched_at"]),
                }
                for row in state_rows
            ],
        )


def _fixture_int(value: object) -> int:
    if isinstance(value, (str, int, float)):
        return int(value)
    raise TypeError(f"fixture value is not integer-compatible: {value!r}")


def _fixture_float(value: object) -> float:
    if isinstance(value, (str, int, float)):
        return float(value)
    raise TypeError(f"fixture value is not numeric: {value!r}")


def _fixture_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    raise TypeError(f"fixture value is not a datetime: {value!r}")


def main() -> None:
    # The self-contained demo may need to add its compact global movie catalog.
    # That bootstrap is intentionally owner-only; admin_user remains limited to
    # tenant-scoped materialization and cannot mutate movies or links.
    engine = create_engine(Settings().database_url, future=True)
    try:
        result = seed_demo_personas(engine)
    finally:
        engine.dispose()
    print(
        f"Seeded {result.persona_count} personas, {result.persona_rating_count} persona ratings, "
        f"and {result.background_rating_count} background ratings into tenant {result.tenant_id!r}."
    )
    # Coverage is reported rather than assumed: a database seeded from an image
    # built before the last enrichment run agrees with itself and quietly serves
    # yesterday's snapshot, which is exactly how 24 posters survived a
    # 120-poster fixture for a day.
    print(
        f"Catalog snapshot: {result.catalog_movie_count} visible titles, "
        f"{result.reviewed_movie_count} reviewed, "
        f"{result.poster_movie_count} with posters, "
        f"{result.detail_movie_count} with detail payloads."
    )


def _release_year_from_title(title: str) -> int | None:
    if len(title) >= 6 and title.endswith(")") and title[-5:-1].isdigit():
        return int(title[-5:-1])
    return None


def _catalog_release_year(title: str) -> int | None:
    """The parsed year, or NULL when the catalog's check constraint would reject it."""
    year = _release_year_from_title(title)
    if year is None or not _CATALOG_MIN_YEAR <= year <= _CATALOG_MAX_YEAR:
        return None
    return year


def _sort_title(title: str) -> str:
    value = title[:-7] if _release_year_from_title(title) is not None else title
    normalized = " ".join(value.casefold().split())
    for article in ("the ", "an ", "a "):
        if normalized.startswith(article):
            return f"{normalized[len(article):]}, {article.strip()}"
    return normalized


if __name__ == "__main__":
    main()
