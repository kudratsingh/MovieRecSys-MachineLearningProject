from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, create_engine, text

from src.evaluation.protocol import COLD_START_THRESHOLD
from synthetic.personas.enrich_posters import poster_url_shape_error
from synthetic.personas.seed import (
    load_demo_catalog,
    load_movielens_catalog,
    load_personas,
    seed_demo_personas,
)
from synthetic.smoke.demo import DemoSmokeError, check_catalog_coverage


def _fixture_engine() -> Engine:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text('CREATE TABLE movies ("movieId" INTEGER PRIMARY KEY, title TEXT, genres TEXT)')
        )
        connection.execute(
            text(
                "CREATE TABLE movie_catalog_metadata ("
                "movie_id INTEGER PRIMARY KEY, sort_title TEXT, release_year INTEGER, "
                "poster_url TEXT, overview TEXT, details TEXT, metadata_source TEXT, "
                "source_status TEXT, visible BOOLEAN, "
                "source_updated_at TEXT DEFAULT CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text('CREATE TABLE links ("movieId" INTEGER PRIMARY KEY, "tmdbId" TEXT)')
        )
        connection.execute(
            text(
                'CREATE TABLE ratings (tenant_id TEXT, "userId" INTEGER, '
                '"movieId" INTEGER, rating FLOAT, timestamp INTEGER)'
            )
        )
        connection.execute(
            text(
                "CREATE TABLE demo_personas (tenant_id TEXT, user_id INTEGER, slug TEXT, "
                "display_name TEXT, description TEXT, sort_order INTEGER, synthetic BOOLEAN, "
                "PRIMARY KEY (tenant_id, user_id))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE user_movie_state ("
                "tenant_id TEXT, user_id INTEGER, movie_id INTEGER, watched_at DATETIME, "
                "rating FLOAT, rating_updated_at DATETIME, watchlisted_at DATETIME, "
                "dismissed_at DATETIME, state_version INTEGER, updated_at DATETIME, "
                "PRIMARY KEY (tenant_id, user_id, movie_id))"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE user_feedback_events ("
                "tenant_id TEXT, event_id TEXT, actor_user_id TEXT, user_id INTEGER, "
                "movie_id INTEGER, action TEXT, old_value JSON, new_value JSON, "
                "state_version INTEGER, outcome TEXT, created_at DATETIME, "
                "PRIMARY KEY (tenant_id, event_id))"
            )
        )
    return engine


def test_persona_fixture_has_stable_required_profiles() -> None:
    tenant_id, personas = load_personas()

    assert tenant_id == "demo"
    assert [persona.slug for persona in personas] == [
        "action-fan",
        "drama-fan",
        "eclectic-viewer",
        "cold-start",
    ]
    assert personas[0].history != personas[1].history
    # Every warm persona sits at or above ADR 0001's threshold, because the demo
    # smoke check, the k6 gate's `learned` assertion, `src/release/verify.py`
    # V-5 and the browser journeys all require them on the learned path. The two
    # 12-title personas carry a deliberate margin over the boundary so a single
    # dismissal in a journey cannot tip them onto the popularity fallback.
    assert len(personas[0].history) == 12
    assert len(personas[1].history) == 12
    assert len(personas[2].history) == 11
    assert min(len(persona.history) for persona in personas[:3]) >= COLD_START_THRESHOLD
    assert personas[3].history == ()


def test_demo_catalog_is_stable_and_covers_every_history_movie() -> None:
    _, personas = load_personas()
    movies, background_user_ids = load_demo_catalog()

    catalog_ids = {movie.movie_id for movie in movies}
    history_ids = {movie_id for persona in personas for movie_id in persona.history}
    assert len(movies) == 120
    assert len(background_user_ids) == 5
    assert history_ids <= catalog_ids
    assert all(movie.tmdb_id is None or movie.tmdb_id.isdigit() for movie in movies)
    # Every visible title carries a poster. Browse renders straight from this
    # fixture and never calls TMDB at request time, so a title without a poster
    # URL here is a permanent placeholder in the product.
    assert sum(movie.poster_url is not None for movie in movies) == 120
    assert all(poster_url_shape_error(movie.poster_url) is None for movie in movies)
    # Every title also carries a synopsis now. The 24 reviewed sentences are
    # still hand-written; the other 96 came from the same offline TMDB pass that
    # filled the posters, which is what takes `source_status` to 'complete' and
    # retires the "Partial details" eyebrow from real catalog rows. The
    # partial/unavailable states keep their coverage in the fixture-mode preview
    # (docs/frontend/catalog-contract.md), where they are meant to live.
    assert sum(movie.overview is not None for movie in movies) == 120


def test_movielens_snapshot_covers_the_whole_catalog_and_contains_the_fixture() -> None:
    """The floor the reviewed fixture is laid on: every MovieLens-25M title.

    W27's diagnosis in one assertion. A retriever fitted on the full dataset
    ranks ids from a 34,461-item vocabulary; while the demo database held 120
    movies, none of them were rows, hydration returned nothing, and the API
    answered with the popularity fallback. The snapshot is what makes those ids
    hydratable.
    """
    reviewed, _ = load_demo_catalog()
    snapshot = load_movielens_catalog()

    assert len(snapshot) == 62423
    assert {movie.movie_id for movie in reviewed} <= {movie.movie_id for movie in snapshot}
    # Genres and TMDB ids are the two columns the serving path joins on, so a
    # snapshot that dropped either would seed a catalog that hydrates without a
    # poster or ranks without a genre affinity.
    assert all(movie.genres for movie in snapshot)
    assert sum(movie.tmdb_id is not None for movie in snapshot) == 62316
    # Nothing here carries artwork: enriching 62k titles is out of scope, and
    # pretending otherwise is what would make Browse look broken rather than
    # sparse.
    assert not any(movie.poster_url or movie.overview or movie.details for movie in snapshot)
    # Migration 0011 rejects a release year below 1878, and MovieLens ships one
    # ("Passage de Venus (1874)"). It is seeded with a NULL year, not dropped.
    assert all(
        movie.release_year is None or 1878 <= movie.release_year <= 2100 for movie in snapshot
    )
    assert next(movie for movie in snapshot if movie.movie_id == 148054).release_year is None


def test_seed_is_idempotent_and_preserves_cold_start() -> None:
    engine = _fixture_engine()
    first = seed_demo_personas(engine)
    second = seed_demo_personas(engine)

    with engine.connect() as connection:
        persona_count = connection.scalar(
            text("SELECT COUNT(*) FROM demo_personas WHERE tenant_id = 'demo'")
        )
        rating_count = connection.scalar(
            text("SELECT COUNT(*) FROM ratings WHERE tenant_id = 'demo'")
        )
        cold_rating_count = connection.scalar(
            text(
                "SELECT COUNT(*) FROM ratings "
                "WHERE tenant_id = 'demo' AND \"userId\" = 900000104"
            )
        )
        state_count = connection.scalar(
            text("SELECT COUNT(*) FROM user_movie_state WHERE tenant_id = 'demo'")
        )
        event_count = connection.scalar(
            text("SELECT COUNT(*) FROM user_feedback_events WHERE tenant_id = 'demo'")
        )

    assert first == second
    assert persona_count == first.persona_count
    assert rating_count == first.persona_rating_count + first.background_rating_count
    assert cold_rating_count == 0
    assert state_count == rating_count
    assert event_count == rating_count
    assert first.catalog_movie_count == 62423
    assert first.reviewed_movie_count == 120
    assert first.recommendable_movie_count == 120
    assert first.poster_movie_count == 120
    assert first.background_rating_count == 480
    engine.dispose()


def test_seed_does_not_overwrite_existing_full_catalog_rows() -> None:
    engine = _fixture_engine()
    with engine.begin() as connection:
        connection.execute(
            text(
                'INSERT INTO movies ("movieId", title, genres) '
                "VALUES (1, 'Canonical full-ingest title', 'Canonical')"
            )
        )
        connection.execute(text('INSERT INTO links ("movieId", "tmdbId") VALUES (1, \'999\')'))

    seed_demo_personas(engine)

    with engine.connect() as connection:
        title = connection.scalar(text('SELECT title FROM movies WHERE "movieId" = 1'))
        tmdb_id = connection.scalar(text('SELECT "tmdbId" FROM links WHERE "movieId" = 1'))
    assert title == "Canonical full-ingest title"
    assert tmdb_id == "999"
    engine.dispose()


def test_reseeding_refreshes_fixture_owned_catalog_metadata() -> None:
    """A database seeded before the fixture was enriched must pick the new URLs up."""
    engine = _fixture_engine()
    seed_demo_personas(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE movie_catalog_metadata "
                "SET poster_url = NULL, overview = NULL, source_status = 'partial' "
                "WHERE movie_id = 1"
            )
        )
        # The shape an older seed left behind: a links row that exists but has
        # never carried a TMDB id.
        connection.execute(text('UPDATE links SET "tmdbId" = NULL WHERE "movieId" = 1'))

    seed_demo_personas(engine)

    with engine.connect() as connection:
        poster_url = connection.scalar(
            text("SELECT poster_url FROM movie_catalog_metadata WHERE movie_id = 1")
        )
        overview = connection.scalar(
            text("SELECT overview FROM movie_catalog_metadata WHERE movie_id = 1")
        )
        status = connection.scalar(
            text("SELECT source_status FROM movie_catalog_metadata WHERE movie_id = 1")
        )
        tmdb_id = connection.scalar(text('SELECT "tmdbId" FROM links WHERE "movieId" = 1'))

    assert poster_url is not None and poster_url.startswith("https://image.tmdb.org/t/p/w500/")
    assert overview is not None
    assert status == "complete"
    assert tmdb_id == "862"
    engine.dispose()


def test_reseeding_refreshes_the_fixture_owned_detail_payload() -> None:
    """The detail payload is fixture-owned, so a re-seed is how it lands.

    A database seeded before ``enrich_details.py`` ran carries a NULL here and
    would keep carrying one forever under an ``ON CONFLICT DO NOTHING``: the
    demo would agree with itself and serve a detail page with no trailer.
    """
    engine = _fixture_engine()
    seed_demo_personas(engine)
    with engine.begin() as connection:
        connection.execute(text("UPDATE movie_catalog_metadata SET details = NULL"))

    result = seed_demo_personas(engine)

    with engine.connect() as connection:
        stored = connection.scalar(
            text("SELECT details FROM movie_catalog_metadata WHERE movie_id = 1")
        )
        filled = connection.scalar(
            text("SELECT COUNT(*) FROM movie_catalog_metadata WHERE details IS NOT NULL")
        )

    assert filled == 120
    assert result.detail_movie_count == 120
    payload = json.loads(str(stored))
    assert payload["trailer"]["provider"] == "youtube"
    assert payload["cast"] and payload["directors"]
    # Nothing here is tenant-owned: movie facts are the same for every tenant,
    # which is why this column lives on the shared snapshot (migration 0011).
    assert "tenant" not in str(stored).lower()
    engine.dispose()


def test_seed_persists_reviewed_metadata_without_live_enrichment() -> None:
    engine = _fixture_engine()
    seed_demo_personas(engine)

    with engine.connect() as connection:
        visible = connection.scalar(
            text("SELECT COUNT(*) FROM movie_catalog_metadata WHERE visible IS TRUE")
        )
        complete = connection.scalar(
            text("SELECT COUNT(*) FROM movie_catalog_metadata " "WHERE source_status = 'complete'")
        )
        with_poster = connection.scalar(
            text("SELECT COUNT(*) FROM movie_catalog_metadata WHERE poster_url IS NOT NULL")
        )
        source = connection.scalar(
            text("SELECT metadata_source FROM movie_catalog_metadata WHERE movie_id = 1")
        )
        # A title only the MovieLens snapshot knows about: visible, so Browse
        # renders it, and honest about having nothing behind it.
        unreviewed = connection.execute(
            text(
                "SELECT metadata_source, source_status, poster_url, visible "
                "FROM movie_catalog_metadata WHERE movie_id = 148054"
            )
        ).one()

    assert visible == 62423
    assert with_poster == 120
    # 'complete' means poster *and* overview. Both are filled for every reviewed
    # title, so a 'partial' row among those means the fixture regressed, not
    # that the snapshot is merely young. The other 62,303 are 'unavailable' and
    # say so.
    assert complete == 120
    assert source == "reviewed-fixture"
    assert unreviewed.metadata_source == "movielens"
    assert unreviewed.source_status == "unavailable"
    assert unreviewed.poster_url is None
    assert unreviewed.visible
    engine.dispose()


def test_seed_keeps_the_reviewed_title_and_genres_over_the_bulk_snapshot() -> None:
    """The reviewed fixture is editorial, and a 62k-row bulk load must not undo it.

    Raw MovieLens spells 31 of these titles differently ("Usual Suspects, The")
    and carries an ``IMAX`` genre on three of them. Both are deliberate edits,
    and the genre one is load-bearing beyond the product: `demo_artifacts` reads
    `movies` unfiltered to build its feature index, so a genre string moving
    under it moves the committed ranker.
    """
    engine = _fixture_engine()
    seed_demo_personas(engine)

    with engine.connect() as connection:
        title = connection.scalar(text('SELECT title FROM movies WHERE "movieId" = 50'))
        genres = connection.scalar(text('SELECT genres FROM movies WHERE "movieId" = 150'))

    assert title == "The Usual Suspects (1995)"
    assert genres == "Adventure|Drama"
    engine.dispose()


# --- The staleness gate the smoke run performs --------------------------------
#
# S5's failure mode had nothing to do with the code: the fixture reached 120
# posters while the running database still held a 24-poster snapshot, and every
# test stayed green because nothing compared the two. These cover the check that
# now does, driven against a mocked catalog endpoint rather than a live stack.


def _catalog_page(items: list[dict[str, Any]], next_cursor: str | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "tenant_id": "demo",
            "user_id": 900000101,
            "items": items,
            "page": {"next_cursor": next_cursor, "has_more": next_cursor is not None},
        },
    )


def _served(movie_id: int, *, poster: bool = True, overview: bool = True) -> dict[str, Any]:
    return {
        "movie_id": movie_id,
        "poster_url": f"https://image.tmdb.org/t/p/w500/{movie_id}.jpg" if poster else None,
        "overview": "A synopsis." if overview else None,
    }


def _detail_page(movie_id: int, *, details: bool = True) -> httpx.Response:
    """The detail read the coverage probe makes, as the API answers it."""
    return httpx.Response(
        200,
        json={
            "tenant_id": "demo",
            "user_id": 900000101,
            "item": {
                "movie_id": movie_id,
                "poster_url": None,
                "overview": None,
                "details": ({"trailer": {"provider": "youtube"}} if details else None),
            },
        },
    )


def _route_detail_probe(request: httpx.Request) -> httpx.Response | None:
    """Answer the detail probe; leave catalog pages to the caller's handler."""
    if "/movies/" in request.url.path:
        return _detail_page(int(request.url.path.rsplit("/", 1)[-1]))
    return None


def _coverage(handler: Any) -> Any:
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return check_catalog_coverage(
            client,
            api_url="http://api.test",
            headers={"Authorization": "Bearer service-token"},
        )


def test_catalog_coverage_passes_on_a_snapshot_that_matches_the_fixture() -> None:
    movies, _ = load_demo_catalog()
    pages = [
        [_served(movie.movie_id) for movie in movies[index : index + 48]]
        for index in range(0, len(movies), 48)
    ]
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer service-token"
        probe = _route_detail_probe(request)
        if probe is not None:
            return probe
        assert request.url.path == "/users/900000101/catalog"
        requested.append(str(request.url.params))
        page_index = int(request.url.params.get("cursor", "0"))
        cursor = str(page_index + 1) if page_index + 1 < len(pages) else None
        return _catalog_page(pages[page_index], cursor)

    coverage = _coverage(handler)

    assert coverage.fixture_movie_count == 120
    assert coverage.served_movie_count == 120
    assert coverage.served_poster_count == 120
    assert coverage.served_overview_count == 120
    # Every fixture title now carries a detail payload, so the probe picks the
    # lowest movie id and proves the served row has one too.
    assert coverage.detail_probe_movie_id == min(movie.movie_id for movie in movies)
    # The whole fixture is walked, and the walk stops when the cursor does.
    assert len(requested) == len(pages)
    # Popularity, and not by preference: the demo database holds the full 62,423
    # title catalog and only the reviewed titles carry seeded interactions, so
    # the default title sort would walk eight pages of the alphabet, see none of
    # the rows this check is about, and pass without checking anything.
    assert all("sort=popular" in params for params in requested)


def test_catalog_coverage_fails_when_the_detail_payload_is_missing() -> None:
    """The staleness this release introduces: posters fine, detail page empty.

    An API image built before the enrichment ran seeds a snapshot with no
    ``details`` at all. Every poster is where it should be, so the catalog walk
    is happy; the detail page is the one that is a year behind.
    """
    movies, _ = load_demo_catalog()

    def handler(request: httpx.Request) -> httpx.Response:
        if "/movies/" in request.url.path:
            return _detail_page(int(request.url.path.rsplit("/", 1)[-1]), details=False)
        return _catalog_page([_served(movie.movie_id) for movie in movies[:48]])

    with pytest.raises(DemoSmokeError) as failure:
        _coverage(handler)

    message = str(failure.value)
    assert "make demo-seed" in message
    assert "without the detail payload" in message


def test_catalog_coverage_fails_on_the_pre_backfill_snapshot() -> None:
    """The exact state the running demo was in: rows present, artwork missing."""
    movies, _ = load_demo_catalog()

    def handler(request: httpx.Request) -> httpx.Response:
        return _catalog_page(
            [
                _served(movie.movie_id, poster=index >= 24, overview=index >= 24)
                for index, movie in enumerate(movies[:48])
            ]
        )

    with pytest.raises(DemoSmokeError) as failure:
        _coverage(handler)

    message = str(failure.value)
    assert "make demo-seed" in message
    assert "24 titles are served without a poster" in message
    assert "24 titles are served without an overview" in message


def test_catalog_coverage_only_judges_titles_the_fixture_owns() -> None:
    """A deployment whose catalog is wider than the fixture is not asked about the rest."""
    movies, _ = load_demo_catalog()

    def handler(request: httpx.Request) -> httpx.Response:
        probe = _route_detail_probe(request)
        if probe is not None:
            return probe
        return _catalog_page(
            [_served(movies[0].movie_id)] + [_served(10_000_001, poster=False, overview=False)]
        )

    coverage = _coverage(handler)

    assert coverage.served_movie_count == 1
    assert coverage.served_poster_count == 1
