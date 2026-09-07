# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The Charts page's Listening Behavior card: bucketing and ratio maths for
the raw plays.{platform,conn_country,reason_end,shuffle,offline,incognito}
columns (Database/queries/plays.py's getBehavioralCounts).

Pure (no DB, no Flask) - mirrors services/dashboard_trends.py. Every listener-
recorded play (the live path) leaves these columns NULL; only the two import
paths populate them (see BEHAVIORAL_COLUMNS in Database/db.py). That is why
every ratio here is reported over `known` (the non-NULL rows), never `total`
(all plays in range) - a range mixing imported and live rows is the normal
case, not an edge one.

reason_end and platform are both raw Spotify export strings with no label map
upstream, so both go through a bucketing function here before they reach a
chart. reason_end is unrelated to plays.is_skip: `is_skip` is a threshold on
how much of the track played (import_service.py), while reason_end says HOW a
play ended (skip-forward, track finished, app closed...) - a long play can
still end in "fwdbtn", and a live-recorded skip has reason_end NULL. That is
why this card's chart is titled "How plays ended", never "Skips"."""

# Percentages throughout are reported as 0-100 floats, one decimal place.
PERCENT_MULTIPLIER = 100
PCT_DECIMALS = 1

# The three behavioral flags this card reports a ratio for. Order here is the
# order buildListeningBehavior's `flags` dict is built in, which is also the
# order the three stat tiles render in.
BEHAVIOR_FLAG_NAMES = ("shuffle", "offline", "incognito")

# reason_end/platform/conn_country rows are capped to their highest counts so
# a chart with dozens of near-zero long-tail values doesn't drown out the ones
# that matter.
TOP_REASONS = 8
TOP_COUNTRIES = 8

# NULL reason_end (a live-recorded play never sets it) folds into this bucket,
# same as a NULL/unrecognised conn_country - "no data", not "played from
# nowhere". Kept distinct from platform's "Other" bucket (an unrecognised but
# PRESENT string), since the two mean different things: one is missing data,
# the other is data this app doesn't have a label for yet.
UNKNOWN_LABEL = "Unknown"

# reason_end values observed in real exports (see the plan doc's field notes):
# fwdbtn/backbtn/clickrow are user-initiated skips, trackdone/endplay are the
# track finishing or playback stopping normally, remote/appload/logout are
# session-level events (a different device took over, the app (re)loaded, the
# account signed out) rather than anything about this one play.
REASON_END_LABELS = {
    "fwdbtn": "Skipped forward",
    "backbtn": "Skipped backward",
    "clickrow": "Selected another track",
    "trackdone": "Track finished",
    "endplay": "Playback ended",
    "remote": "Remote control",
    "appload": "App loaded",
    "logout": "Logged out",
}
# Several unexpected-exit variants exist across export vintages
# ("unexpected-exit", "unexpected-exit-while-loading", ...) - all fold into
# one bucket rather than each claiming its own row in a top-N chart.
REASON_END_UNEXPECTED_EXIT_PREFIX = "unexpected-exit"
REASON_END_UNEXPECTED_EXIT_LABEL = "App closed unexpectedly"

# bucketPlatform's substrings, matched case-insensitively. Named rather than
# inlined so the precedence order below (the only thing that decides a
# compound string like "Windows 10 (...; web_player)") is legible as a list of
# names, not a wall of quoted literals.
PLATFORM_ANDROID_SUBSTR = "android"
PLATFORM_IOS_SUBSTR = "ios"
PLATFORM_IPHONE_SUBSTR = "iphone"
PLATFORM_IPAD_SUBSTR = "ipad"
PLATFORM_WINDOWS_SUBSTR = "windows"
PLATFORM_OSX_SUBSTR = "osx"
PLATFORM_MACOS_SUBSTR = "macos"
PLATFORM_LINUX_SUBSTR = "linux"
PLATFORM_WEB_PLAYER_SUBSTR = "web_player"

PLATFORM_ANDROID_LABEL = "Android"
PLATFORM_IOS_LABEL = "iOS"
PLATFORM_WINDOWS_LABEL = "Windows"
PLATFORM_MACOS_LABEL = "macOS"
PLATFORM_LINUX_LABEL = "Linux"
PLATFORM_WEB_LABEL = "Web player"
PLATFORM_OTHER_LABEL = "Other"


def bucketReasonEnd(raw: str | None) -> str:
    """Raw plays.reason_end -> a display bucket.

    NULL (never set by the live listener) becomes UNKNOWN_LABEL. Every
    "unexpected-exit*" variant folds into one bucket. Anything else falls
    back to REASON_END_LABELS, and failing that to the raw code itself,
    title-cased with separators turned to spaces - so a future Spotify export
    code this app has no label for yet still reads as words, not
    "some_new_code"."""
    if raw is None:
        return UNKNOWN_LABEL
    if raw.startswith(REASON_END_UNEXPECTED_EXIT_PREFIX):
        return REASON_END_UNEXPECTED_EXIT_LABEL
    if raw in REASON_END_LABELS:
        return REASON_END_LABELS[raw]
    return raw.replace("_", " ").replace("-", " ").title()


def bucketPlatform(raw: str | None) -> str:
    """Raw plays.platform -> "Android"/"iOS"/"Windows"/"macOS"/"Linux"/
    "Web player"/"Other" (None and any unrecognised string both land in
    "Other" - unlike reason_end, an unlabelled platform string is DATA, not
    missing data, so it does not deserve its own "Unknown" bucket).

    Case-insensitive substring match, checked in the fixed order below. Order
    matters for a compound string: some older exports carry both an OS and a
    player hint in one field (e.g. "Windows 10 (...; web_player)"), and the
    four OS/device checks run before the web-player check so that shape
    buckets to the OS, not the player."""
    if not raw:
        return PLATFORM_OTHER_LABEL
    lowered = raw.lower()
    if PLATFORM_ANDROID_SUBSTR in lowered:
        return PLATFORM_ANDROID_LABEL
    if (PLATFORM_IOS_SUBSTR in lowered or PLATFORM_IPHONE_SUBSTR in lowered
            or PLATFORM_IPAD_SUBSTR in lowered):
        return PLATFORM_IOS_LABEL
    if PLATFORM_WINDOWS_SUBSTR in lowered:
        return PLATFORM_WINDOWS_LABEL
    if PLATFORM_OSX_SUBSTR in lowered or PLATFORM_MACOS_SUBSTR in lowered:
        return PLATFORM_MACOS_LABEL
    if PLATFORM_LINUX_SUBSTR in lowered:
        return PLATFORM_LINUX_LABEL
    if PLATFORM_WEB_PLAYER_SUBSTR in lowered:
        return PLATFORM_WEB_LABEL
    return PLATFORM_OTHER_LABEL


def _bucketAndSum(rawPairs, bucketFn, topN=None):
    """[(raw, count), ...] -> [(bucketLabel, totalCount), ...], summed across
    every raw value that buckets to the same label, sorted by count desc
    (ties broken alphabetically for a deterministic chart), optionally
    truncated to the `topN` HIGHEST counts."""
    totals: dict[str, int] = {}
    for raw, count in rawPairs:
        label = bucketFn(raw)
        totals[label] = totals.get(label, 0) + (count or 0)
    ordered = sorted(totals.items(), key=lambda pair: (-pair[1], pair[0]))
    return ordered[:topN] if topN is not None else ordered


def _percent(part: int, whole: int) -> float | None:
    """part/whole as a 0-100 float, or None when whole is 0 (rather than a
    misleading 0% - "no data" and "measured at zero" must not look the
    same)."""
    if not whole:
        return None
    return round(part / whole * PERCENT_MULTIPLIER, PCT_DECIMALS)


def buildListeningBehavior(raw: dict) -> dict:
    """Repository.getBehavioralCounts' raw dict -> the Charts card's shape:
    `{hasData, flags, reasonEnd, platforms, countries, unknownReasonShare}`.

    `hasData` is false only when NOTHING in the range carries any behavioral
    column - distinct from `total == 0` (no plays at all), and from a range
    full of plays that are all NULL (a live-only stretch): both render the
    "import your history" hint, but for different reasons, and hasData is the
    single flag that covers both.

    `flags[name]` is `{on, known, total, pct, knownPct}` for each of
    BEHAVIOR_FLAG_NAMES - `pct` is `on` over `known` (never `total`), None
    when `known == 0` so a per-flag divide-by-zero never surfaces as a fake
    0%. `knownPct` is `known` over `total`, the second number the card's
    tiles show alongside `pct` ("62% shuffled - of 412 plays with this data
    (40% of 1,030)") so a range mixing imported and live plays never reads as
    if the whole range were measured.

    `raw` is the dict Repository.getBehavioralCounts returns; an empty dict
    is the no-data case. Anything else is a caller bug and fails loudly."""
    total = raw.get("total", 0) or 0

    flags = {}
    knownAny = 0
    for name in BEHAVIOR_FLAG_NAMES:
        flagRaw = raw.get(name) or {}
        known = flagRaw.get("known", 0) or 0
        on = flagRaw.get("on", 0) or 0
        flags[name] = {
            "on": on, "known": known, "total": total,
            "pct": _percent(on, known), "knownPct": _percent(known, total),
        }
        knownAny += known

    reasonRawPairs = raw.get("reasonEnd", [])
    reasonEnd = _bucketAndSum(reasonRawPairs, bucketReasonEnd, topN=TOP_REASONS)
    knownAny += sum(count or 0 for label, count in reasonRawPairs if label is not None)

    platformRawPairs = raw.get("platforms", [])
    platforms = _bucketAndSum(platformRawPairs, bucketPlatform)
    knownAny += sum(count or 0 for label, count in platformRawPairs if label is not None)

    countryRawPairs = raw.get("countries", [])
    countries = _bucketAndSum(countryRawPairs, lambda label: label or UNKNOWN_LABEL, topN=TOP_COUNTRIES)
    knownAny += sum(count or 0 for label, count in countryRawPairs if label is not None)

    reasonTotal = sum(count or 0 for _, count in reasonRawPairs)
    unknownReasonCount = sum(count or 0 for label, count in reasonRawPairs if label is None)

    return {
        "hasData": knownAny > 0,
        "flags": flags,
        "reasonEnd": reasonEnd,
        "platforms": platforms,
        "countries": countries,
        "unknownReasonShare": _percent(unknownReasonCount, reasonTotal),
    }
