"""The taste-match scoring algorithm behind the Compare page.

Extracted verbatim from app.py (behavior-preserving). These are pure functions -
rank-weighted pool-overlap scoring plus the small display helper that marks a
counterpart item as Spotify-linked. app.py's compare route calls
_tasteMatchPercent and _markLinkExternally; its _buildSharedItems (which stays a
method, being bound to the class's _resortByMetric) calls _rankById and
_sharedRankScore. Names keep their leading underscore so the dense scoring
docstrings - which cross-reference each other - move without edits.
"""
import math

# Taste-match weighting: artists dominate - exact-song collisions between
# two top-100s are structurally rare even for very similar listeners (huge
# catalog, low odds), so songs barely count; albums sit between. Genres are
# coarser still (many listeners share broad tags without similar taste), so
# they weigh less than albums - present only when both sides pass the genre
# unlock gate (see genreGatePasses), same bar every other genre surface
# uses. Categories without data on both sides are excluded and the
# remaining weights renormalized.
TASTE_MATCH_WEIGHTS = {"artists": 0.7, "songs": 0.1, "albums": 0.2, "genres": 0.15}
# Rank-weighted overlap normalizes against an "ideal" match capped at this
# depth rather than the full COMPARE_OVERLAP_POOL_SIZE: requiring near-total
# overlap of a 100-deep pool for 100% meant even two listeners who share
# their entire top 20 favorite artists scored ~34%, since agreement past
# rank ~30 barely matters to how similar two people's taste feels.
TASTE_MATCH_IDEAL_DEPTH = 30
# An exact id match earns DOUBLE the rank discount of its BETTER (shallower)
# side's rank - the "both sides at rank r" shape the taste-match ideal
# normalizes against. The single place the factor is applied is
# _mutualRankScore, taste-match's exact-match credit. (The Top Common
# lists rank by _sharedRankScore instead - see it for why min()-based
# credit is wrong for a display list.)
EXACT_MATCH_CREDIT_FACTOR = 2
# A song/album that ISN'T an exact match still earns this fraction of its own
# rank discount when its primary artist appears in the counterpart's top
# artist pool (see _rankWeightedOverlap) - loving the same ARTIST without
# happening to share the exact same song/album is real taste overlap, not
# zero. Doesn't apply to the artist category itself (no secondary "artist of
# an artist" concept there).
ARTIST_MEDIATED_CREDIT_FACTOR = 0.4
# The final taste-match score (0..1 weighted average across categories) is
# raised to this power before display: a concave response curve, since real
# people rarely share MOST of their top taste even when they genuinely have
# similar taste - a linear score reads as harshly low for overlap that
# actually feels like "we like a lot of the same stuff." Monotonic, so it
# never reorders which of two pairs is the better match, just stretches the
# low-to-mid range upward (raw 0.25 -> ~50%, raw 0.5 -> ~71%).
TASTE_MATCH_CURVE_EXPONENT = 0.6


def _rankWeight(rank: int) -> float:
    """DCG-style discount for a 1-based rank: the #1 spot weighs 1,
    deeper ranks fall off logarithmically."""
    return 1 / math.log2(rank + 1)


def _rankById(pool: list[dict]) -> dict:
    """id -> 1-based rank map of an already-ordered pool - the lookups
    _mutualRankScore/_sharedRankScore consume. The ORDERING stays the
    caller's choice: _rankWeightedOverlap must trust the incoming
    pool's own order (its genre pools are bare {"id": genre} dicts
    with no metrics to re-derive one from), while _buildSharedItems
    re-sorts by plays first (see its docstring for why)."""
    return {item["id"]: rank for rank, item in enumerate(pool, start=1)}


def _mutualRankScore(myRank: int, theirRank: int) -> float:
    """Taste-match's exact-match credit: EXACT_MATCH_CREDIT_FACTOR x
    the rank discount (see _rankWeight) of the BETTER (shallower) of
    the two ranks for the same item - _rankWeightedOverlap's per-item
    credit AND its ideal normalizer (_mutualRankScore(r, r)) both use
    it; see that docstring for the mutual-favorite rationale. The Top
    Common lists deliberately rank by _sharedRankScore instead."""
    return EXACT_MATCH_CREDIT_FACTOR * _rankWeight(min(myRank, theirRank))


def _sharedRankScore(myRank: int, theirRank: int) -> float:
    """Ranking score for a Top Common list entry: the SUM of both
    sides' rank discounts (see _rankWeight), so both users' engagement
    counts. Deliberately NOT _mutualRankScore's min() shape: min()
    ignores the weaker side entirely, so a one-sided favorite (my #1,
    their #200) would score like a true #1/#1 mutual favorite and
    outrank a genuine #2/#2 - fine for taste-match's aggregate (its
    ideal normalizer is built on the same-rank shape), wrong for a
    list literally titled "common" (see
    test_shared_list_one_sided_favorite_loses_to_true_mutual_item).
    The sum still lets one side's #1 carry a moderate counterpart
    rank past two lukewarm mid-ranks (see
    test_shared_list_ranks_by_mutual_favorite_not_raw_combined_plays)."""
    return _rankWeight(myRank) + _rankWeight(theirRank)


def _primaryArtistId(item: dict) -> str | None:
    """The first-listed (primary) artist's id for a song/album pool item -
    track_artists.position 0, i.e. how Spotify itself orders credited
    artists - or None if the item somehow carries no artists."""
    artists = item.get("artists") or []
    return artists[0]["id"] if artists else None


def _rankWeightedOverlap(myPool, theirPool, myArtistIds=None, theirArtistIds=None) -> float | None:
    """0..1 rank-weighted overlap of two ranked pools, normalized against
    the score two pools would reach if they agreed on their top
    TASTE_MATCH_IDEAL_DEPTH items - so a shared #1 counts far more than
    a shared #90, and matching core taste can reach 100% without also
    requiring overlap across the entire deep pool. Clamped to 1 since
    overlap (or artist-mediated credit, see below) can push the raw
    ratio above it. None when either side is empty, so the category can
    be excluded rather than scored 0.

    An exact id match contributes _mutualRankScore:
    EXACT_MATCH_CREDIT_FACTOR x the rank discount of its BETTER
    (shallower/lower-numbered) side's rank, not the sum of both sides'
    discounts - matches `ideal`'s own shape (_mutualRankScore(r, r),
    the case where both sides tie at the same rank r), and means a
    mutual favorite ranked #3 by one person and #40 by the other still
    counts close to a #3/#3 match instead of being dragged down by
    whichever side ranks it deeper.

    When myArtistIds/theirArtistIds are given (songs/albums only - the
    artist category has no secondary "artist of an artist" concept), a
    non-exact item still earns ARTIST_MEDIATED_CREDIT_FACTOR of its own
    rank discount when its primary artist (see _primaryArtistId)
    appears in the counterpart's top artist pool."""
    if not myPool or not theirPool:
        return None
    myRanks = _rankById(myPool)
    theirRanks = _rankById(theirPool)
    exactIds = myRanks.keys() & theirRanks.keys()
    actual = sum(_mutualRankScore(myRanks[itemId], theirRanks[itemId]) for itemId in exactIds)

    if myArtistIds is not None and theirArtistIds is not None:
        myById = {item["id"]: item for item in myPool}
        theirById = {item["id"]: item for item in theirPool}
        for itemId, rank in myRanks.items():
            if itemId in exactIds:
                continue
            artistId = _primaryArtistId(myById[itemId])
            if artistId is not None and artistId in theirArtistIds:
                actual += ARTIST_MEDIATED_CREDIT_FACTOR * _rankWeight(rank)
        for itemId, rank in theirRanks.items():
            if itemId in exactIds:
                continue
            artistId = _primaryArtistId(theirById[itemId])
            if artistId is not None and artistId in myArtistIds:
                actual += ARTIST_MEDIATED_CREDIT_FACTOR * _rankWeight(rank)

    idealDepth = min(len(myPool), len(theirPool), TASTE_MATCH_IDEAL_DEPTH)
    ideal = sum(_mutualRankScore(rank, rank) for rank in range(1, idealDepth + 1))
    return min(1.0, actual / ideal)


def _tasteMatchPercent(my, their, myGenrePool=None, theirGenrePool=None) -> int | None:
    """One headline number for how much two users' taste overlaps: the
    rank-weighted pool overlap per category (with artist-mediated credit
    for songs/albums - see _rankWeightedOverlap), weighted by
    TASTE_MATCH_WEIGHTS and passed through a concave response curve
    (see TASTE_MATCH_CURVE_EXPONENT). None when no category has data on
    both sides - the UI hides the badge instead of showing a misleading
    0%.

    myGenrePool/theirGenrePool are {genre: plays} distributions (see
    Database.getGenreDistribution), or None/empty when the caller's
    genre unlock gate hasn't passed for both sides - the genre category
    behaves like "artists" (exact string match only, no secondary
    mediation) and is naturally excluded by _rankWeightedOverlap when
    either pool is empty."""
    myArtistIds = {a["id"] for a in my["topArtistsPool"]}
    theirArtistIds = {a["id"] for a in their["topArtistsPool"]}
    myGenresPool = [{"id": genre} for genre in (myGenrePool or {})]
    theirGenresPool = [{"id": genre} for genre in (theirGenrePool or {})]
    categories = {
        "artists": (my["topArtistsPool"], their["topArtistsPool"], None, None),
        "songs": (my["topSongsPool"], their["topSongsPool"], myArtistIds, theirArtistIds),
        "albums": (my["topAlbumsPool"], their["topAlbumsPool"], myArtistIds, theirArtistIds),
        "genres": (myGenresPool, theirGenresPool, None, None),
    }
    parts = []
    for kind, (myPool, theirPool, myAIds, theirAIds) in categories.items():
        fraction = _rankWeightedOverlap(myPool, theirPool, myAIds, theirAIds)
        if fraction is not None:
            parts.append((fraction, TASTE_MATCH_WEIGHTS[kind]))
    if not parts:
        return None
    raw = sum(fraction * weight for fraction, weight in parts) / sum(weight for _, weight in parts)
    return round(100 * raw ** TASTE_MATCH_CURVE_EXPONENT)


def _markLinkExternally(items: list[dict], playedIds: set) -> None:
    """In place: sets item['linkExternally'] so _track_card.html (and
    _compare_stats_table.html's theirCell macro) link this counterpart
    item to Spotify only when it's NOT in `playedIds` - i.e. only when
    the viewer has no data of their own for it."""
    for item in items:
        item["linkExternally"] = item["id"] not in playedIds
