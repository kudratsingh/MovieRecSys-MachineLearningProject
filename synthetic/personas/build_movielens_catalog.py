"""Rebuild the committed full MovieLens catalog snapshot from the raw CSVs.

``catalog.json`` is the *reviewed* 120-title fixture: hand-checked titles,
posters, synopses and detail payloads. This script produces its complement —
``movielens-catalog.csv.gz``, every one of MovieLens-25M's 62,423 titles with
the two fields the serving path actually joins on (``movies.genres`` and
``links."tmdbId"``) and nothing else.

Why a committed snapshot rather than reading ``data/raw/ml-25m`` at seed time:
the demo runbook promises a clean checkout is enough ("downloading or ingesting
the 25M dataset is not required for the walkthrough"), and the seeder runs
inside the API image, which copies ``synthetic/`` and never ``data/``. A seeder
that loaded the catalog only when the DVC payload happened to be present would
make the demo database — and therefore the reproducibility gate that trains off
it — depend on which machine ran it. So the snapshot is a fixture like every
other fixture here, and this script is how it is regenerated::

    dvc pull data/raw/ml-25m.dvc
    python -m synthetic.personas.build_movielens_catalog
    git diff --stat synthetic/personas/movielens-catalog.csv.gz

Deterministic to the byte: rows are sorted by ``movieId``, the gzip header
carries no mtime, and the compression level is pinned. Re-running it against
the same MovieLens release rewrites the same file, so a non-empty diff means
the input changed.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
from pathlib import Path

SNAPSHOT_PATH = Path(__file__).parent / "movielens-catalog.csv.gz"
DEFAULT_RAW_DIR = Path("data/raw/ml-25m")
SNAPSHOT_COLUMNS = ("movieId", "title", "genres", "tmdbId")


def build_snapshot_bytes(raw_dir: Path) -> bytes:
    """Join ``movies.csv`` and ``links.csv`` into the snapshot's CSV bytes."""
    movies_path = raw_dir / "movies.csv"
    links_path = raw_dir / "links.csv"
    for path in (movies_path, links_path):
        if not path.exists():
            raise FileNotFoundError(f"expected {path} — run `dvc pull data/raw/ml-25m.dvc` first")

    rows: dict[int, list[str]] = {}
    with movies_path.open(encoding="utf-8", newline="") as movies_file:
        for record in csv.DictReader(movies_file):
            rows[int(record["movieId"])] = [record["title"], record["genres"], ""]
    with links_path.open(encoding="utf-8", newline="") as links_file:
        for record in csv.DictReader(links_file):
            movie_id = int(record["movieId"])
            if movie_id in rows:
                rows[movie_id][2] = record["tmdbId"]

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(SNAPSHOT_COLUMNS)
    for movie_id in sorted(rows):
        writer.writerow([movie_id, *rows[movie_id]])
    return buffer.getvalue().encode("utf-8")


def write_snapshot(raw_dir: Path, destination: Path) -> int:
    payload = build_snapshot_bytes(raw_dir)
    # mtime=0 so the archive is a pure function of its content; a header
    # timestamp would rewrite the file on every run and make the diff useless.
    destination.write_bytes(gzip.compress(payload, compresslevel=9, mtime=0))
    return payload.count(b"\n") - 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--out", type=Path, default=SNAPSHOT_PATH)
    args = parser.parse_args()
    count = write_snapshot(args.raw_dir, args.out)
    print(f"Wrote {count} titles to {args.out} ({args.out.stat().st_size} bytes).")


if __name__ == "__main__":
    main()
