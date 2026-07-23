"""Genre/biography coverage sanitization and the genre-feature unlock gate.

Extracted verbatim from app.py (behavior-preserving). The genre insights on
Charts/Wrapped/Compare only render once the play-weighted Last.fm coverage over
the page's date range clears this gate; every genre surface consumes coverage
and distributions through the resolve*/sanitize* chokepoints here, so a stubbed
or failing db degrades to the locked state instead of crashing template
rendering. app.py imports these names back so its route code and the test suite
(which import several of them by name) are unaffected.
"""
import logging

from Database.database import GENRE_COVERAGE_CATEGORIES

logger = logging.getLogger(__name__)

# The genre-feature unlock gate: genre insights on Charts/Wrapped/Compare only
# render once the play-weighted Last.fm coverage over the page's date range is
# strictly above the overall minimum (mean of the three categories) AND at
# least at the per-category minimum for songs, albums and artists - partial
# data would silently misrepresent someone's taste otherwise.
GENRE_GATE_OVERALL_MIN_PERCENT = 50
GENRE_GATE_CATEGORY_MIN_PERCENT = 30

BIOGRAPHY_COVERAGE_CATEGORIES = ("artist", "album")


def emptyGenreCoverage() -> dict:
    """The all-zeros coverage shape - what guests, empty ranges and sanitize
    failures all resolve to (and the gate always rejects)."""
    coverage = {categoryName: {"covered": 0, "total": 0, "percent": 0.0}
                for categoryName in GENRE_COVERAGE_CATEGORIES}
    coverage["overall"] = {"percent": 0.0}
    return coverage


def _requireNumber(value):
    """The value if it's a real number. Explicit isinstance rather than
    int()/float() coercion: MagicMock happily answers __int__ with 1, which
    would let an unstubbed test db masquerade as real coverage."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"not a number: {value!r}")
    return value


def sanitizeGenreCoverage(coverage) -> dict:
    """`coverage` if it is shaped like Database.getGenreCoverage's result,
    else all zeros. Route code must only ever consume coverage through this:
    stubbed dbs in tests (and any unexpected failure) then degrade to the
    locked state instead of crashing template rendering."""
    try:
        sanitized = {}
        for categoryName in GENRE_COVERAGE_CATEGORIES:
            categoryData = coverage[categoryName]
            sanitizedCategory = {field: _requireNumber(categoryData[field])
                                 for field in ("covered", "total", "percent")}
            # Optional (older callers/stubs don't produce it), but validated
            # like every other field when present - the template only renders
            # the own-tags split when the key survives sanitization.
            if isinstance(categoryData, dict) and "ownPercent" in categoryData:
                sanitizedCategory["ownPercent"] = _requireNumber(categoryData["ownPercent"])
            sanitized[categoryName] = sanitizedCategory
        sanitized["overall"] = {"percent": _requireNumber(coverage["overall"]["percent"])}
        return sanitized
    except (TypeError, KeyError):
        return emptyGenreCoverage()


def resolveGenreCoverage(db, startDate, endDate) -> dict:
    """Sanitized genre coverage for a user db over a range; zeros when the
    lookup fails for any reason (never let the genre gate break a page)."""
    try:
        return sanitizeGenreCoverage(db.getGenreCoverage(startDate=startDate, endDate=endDate))
    except Exception as e:
        logger.warning("Genre coverage lookup failed: %s", e)
        return emptyGenreCoverage()


def resolveGenreDistribution(db, startDate, endDate, limit) -> dict:
    """Genre distribution for a user db over a range; {} when the lookup
    fails or returns a non-dict (stubbed dbs) - the same degradation
    contract as resolveGenreCoverage, so every genre surface consumes
    distributions through this one chokepoint."""
    try:
        distribution = db.getGenreDistribution(startDate=startDate, endDate=endDate, limit=limit)
    except Exception as e:
        logger.warning("Genre distribution lookup failed: %s", e)
        return {}
    return distribution if isinstance(distribution, dict) else {}


def genreGatePasses(coverage: dict) -> bool:
    """The unlock rule on a sanitized coverage dict: overall strictly above
    GENRE_GATE_OVERALL_MIN_PERCENT and every category at or above
    GENRE_GATE_CATEGORY_MIN_PERCENT."""
    if coverage["overall"]["percent"] <= GENRE_GATE_OVERALL_MIN_PERCENT:
        return False
    return all(coverage[categoryName]["percent"] >= GENRE_GATE_CATEGORY_MIN_PERCENT
               for categoryName in GENRE_COVERAGE_CATEGORIES)


def resolveGenreTrends(db, genres, startDate, endDate) -> dict:
    """Monthly genre trend ({"buckets", "series"}) for a user db; the empty
    shape when the lookup fails or returns something unexpected (stubbed dbs) -
    same degradation contract as resolveGenreDistribution."""
    empty = {"buckets": [], "series": []}
    try:
        trends = db.getGenreTrends(genres, startDate=startDate, endDate=endDate)
    except Exception as e:
        logger.warning("Genre trends lookup failed: %s", e)
        return empty
    if not isinstance(trends, dict) or "buckets" not in trends or "series" not in trends:
        return empty
    return trends


def resolveGenreStats(db, genre, startDate, endDate) -> dict:
    """Per-genre stat strip for a user db; zeros when the lookup fails or
    returns a non-dict (stubbed dbs)."""
    empty = {"plays": 0, "listenMs": 0, "firstPlayedTs": None, "sharePercent": 0.0}
    try:
        stats = db.getGenreStats(genre, startDate=startDate, endDate=endDate)
    except Exception as e:
        logger.warning("Genre stats lookup failed: %s", e)
        return empty
    return stats if isinstance(stats, dict) else empty


def resolveTopArtistsForGenre(db, genre, limit, startDate=None, endDate=None) -> list:
    """Top artists for one genre over a date range (all-time when both dates
    are None); [] when the lookup fails or returns a non-list (stubbed dbs)."""
    try:
        artists = db.getTopArtistsForGenre(genre, limit, startDate=startDate, endDate=endDate)
    except Exception as e:
        logger.warning("Top artists for genre lookup failed: %s", e)
        return []
    return artists if isinstance(artists, list) else []


def resolveTopTracksForGenre(db, genre, limit, startDate=None, endDate=None) -> list:
    """Top tracks for one genre over a date range (all-time when both dates
    are None); [] when the lookup fails or returns a non-list (stubbed dbs)."""
    try:
        tracks = db.getTopTracksForGenre(genre, limit, startDate=startDate, endDate=endDate)
    except Exception as e:
        logger.warning("Top tracks for genre lookup failed: %s", e)
        return []
    return tracks if isinstance(tracks, list) else []


def resolveGenreArtistCounts(db, genres) -> dict:
    """{genre: artist count} for a user db; {} when the lookup fails or returns
    a non-dict (stubbed dbs) - same degradation contract as the other genre
    resolvers."""
    try:
        counts = db.getGenreArtistCounts(genres)
    except Exception as e:
        logger.warning("Genre artist counts lookup failed: %s", e)
        return {}
    return counts if isinstance(counts, dict) else {}


def emptyHeatmapGrid() -> list:
    """The all-zeros 7x24 listening-clock grid - what a failed/stubbed genre
    heatmap lookup degrades to."""
    return [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]


def resolveGenreHeatmap(db, genre, startDate=None, endDate=None) -> list:
    """Per-genre listening-clock grid for a user db over a date range (all-time
    when both dates are None); a zeroed 7x24 grid when the lookup fails or
    returns a non-list (stubbed dbs)."""
    try:
        grid = db.getGenreHourOfDayHeatmap(genre, startDate=startDate, endDate=endDate)
    except Exception as e:
        logger.warning("Genre heatmap lookup failed: %s", e)
        return emptyHeatmapGrid()
    return grid if isinstance(grid, list) else emptyHeatmapGrid()


def emptyBiographyCoverage() -> dict:
    """The all-zeros shape for the Overview "Biography Backfill Progress"
    widget - what guests and sanitize failures resolve to, mirroring
    emptyGenreCoverage. Unlike genre coverage this is a plain entity-count
    percentage (has a bio or not), not play-weighted."""
    return {categoryName: {"covered": 0, "total": 0, "percent": 0.0}
            for categoryName in BIOGRAPHY_COVERAGE_CATEGORIES}


def sanitizeBiographyCoverage(coverage) -> dict:
    """`coverage` if it is shaped like Repository.getBiographyCoverage's
    result, else all zeros - same defensive chokepoint as
    sanitizeGenreCoverage, so a stubbed db or unexpected failure degrades
    instead of crashing template rendering."""
    try:
        sanitized = {}
        for categoryName in BIOGRAPHY_COVERAGE_CATEGORIES:
            covered = _requireNumber(coverage[categoryName]["covered"])
            total = _requireNumber(coverage[categoryName]["total"])
            percent = round(covered / total * 100, 1) if total else 0.0
            sanitized[categoryName] = {"covered": covered, "total": total, "percent": percent}
        return sanitized
    except (TypeError, KeyError):
        return emptyBiographyCoverage()


def resolveBiographyCoverage(db, username: str) -> dict:
    """Sanitized biography coverage for a user; zeros when the lookup fails
    for any reason (never let this break the Overview page)."""
    try:
        return sanitizeBiographyCoverage(db.repo.getBiographyCoverage(username))
    except Exception as e:
        logger.warning("Biography coverage lookup failed: %s", e)
        return emptyBiographyCoverage()


def _resolveGenresFor(db, entityId, dbMethodName: str) -> list[str]:
    """Shared degradation contract for the per-item genre lookups below: a
    lookup failure, or a stubbed test db whose genre method was never
    configured (a bare MagicMock() return value), degrades to [] instead of
    breaking every page that renders a track/artist/album card."""
    try:
        genres = getattr(db, dbMethodName)(entityId)
    except Exception as e:
        logger.warning("%s(%r) failed: %s", dbMethodName, entityId, e)
        return []
    return genres if isinstance(genres, list) else []


def resolveGenresForTrack(db, trackId) -> list[str]:
    return _resolveGenresFor(db, trackId, "getGenresForTrack")


def resolveGenresForAlbum(db, albumId) -> list[str]:
    return _resolveGenresFor(db, albumId, "getGenresForAlbum")


def resolveGenresForArtist(db, artistId) -> list[str]:
    return _resolveGenresFor(db, artistId, "getGenresForArtist")
