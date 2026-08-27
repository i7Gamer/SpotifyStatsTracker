# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

import re
import datetime   #< deliberately not from-imported: datetime.datetime/timezone/timedelta read clearest
import time as _time
from os import environ
from os.path import basename
from traceback import extract_tb
from contextlib import suppress
from zoneinfo import ZoneInfo   #< IANA zones, selected via the TZ env var below

DATE_FORMATS = ("%Y-%m-%d", "%Y-%m", "%Y")   #< bare-date forms parseDateString accepts, most specific first

#< the one bare-date form that also survives float() - see convertToDatetime
_BARE_YEAR_RE = re.compile(r"\d{4}")

# The env-var values this project reads as "on", re-exported from config so the
# Database package and the web layer cannot disagree about what "on" means.
#
# This module briefly carried its OWN {"1", "true"} under that same name while
# config.py carried {"1", "true", "yes", "on"} - two different answers to one
# question, both live and both imported by name, so ENABLE_HSTS=yes was on while
# TOTP_AUTO_RECOVER=yes was off. The name had been chosen to "say what it is",
# which is exactly what made the split invisible.
#
# config is the right home rather than the reverse import: it imports NOTHING,
# so it cannot take part in the cycle Database/dbmodule.py exists to break, and
# it is already where the web layer's flags read theirs. Importing this module
# from config would instead drag in Database/__init__.py, which imports
# Database.patches for its spotapi side effects.
#
# ("0" and "false" are deliberately absent: someone who sets a flag to 0 means
# off, and a bare truthiness test on the string reads that as on.)
try:
    from config import TRUTHY_ENV_VALUES  # noqa: E402 - a constant re-export, not a dependency on app state
except ModuleNotFoundError:               # noqa: E402
    # `python Database/utils.py` - the REPL at the foot of this file - puts
    # THIS directory on sys.path, not the repo root, so a top-level module is
    # not importable from here. Every other way in already has the root
    # (wsgi.py and dev.py live there, the Docker CMD runs from /app, and
    # pyproject's `pythonpath = ["."]` covers the whole test suite), which is
    # exactly why nothing caught this: no test can reach the failing case
    # except by spawning an interpreter, which tests/test_import_cycles.py
    # now does.
    #
    # Resolved from __file__ rather than the relative "../" its sibling shim in
    # Database/Importers/AutoImporter.py uses: that one silently depends on the
    # working directory being Database/.
    import sys as _sys                    # noqa: E402
    from pathlib import Path as _Path     # noqa: E402
    _repoRoot = str(_Path(__file__).resolve().parent.parent)
    if _repoRoot not in _sys.path:
        _sys.path.insert(0, _repoRoot)
    from config import TRUTHY_ENV_VALUES  # noqa: E402


def flaskDebugEnabled() -> bool:
    """Whether the operator asked for verbose diagnostics.

    Lives here because it had six spellings across six modules: two identical
    private copies (Database/patches.py and Database/Listeners/spotifyListener.py,
    each with their own TRUTHY_DEBUG_VALUES marked "mirrors Database.database"),
    four inline `_dbmod.os.environ.get(...) in _dbmod.TRUTHY_DEBUG_VALUES` reads
    in the importer and the metadata backfiller, and one bare
    `if os.environ.get("FLASK_DEBUG")` that did not filter for a truthy value at
    all - so FLASK_DEBUG=0 turned that one log ON while turning every other one
    off.

    The importer and backfiller reached the constant through `_dbmod`, the
    late-bound module reference that exists to dodge the Database import cycle
    (see Database/dbmodule.py). They never needed to: this module imports
    nothing but the standard library, so it is safe to import directly from
    anywhere in the package.

    Read per call rather than cached at import: the tests drive this with
    patch.dict(os.environ, ...) around the code under test, and a value frozen
    at import would ignore them."""
    return environ.get("FLASK_DEBUG", "").lower() in TRUTHY_ENV_VALUES


class _SystemLocalTimezone(datetime.tzinfo):
    """The host's local zone, answered per instant instead of frozen.

    The TZ-unset fallback used to be `datetime.now().astimezone().tzinfo`, which
    returns a plain FIXED offset - whatever the offset happened to be at import,
    for the life of the process. Every day boundary, year boundary, streak day
    and calendar cell for a user with no profile timezone was then an hour out
    from the next DST transition until someone restarted the app, silently. Plays
    between 23:00 and 00:00 on Dec 31 land in the wrong year the same way.

    This asks the platform for the offset that applied AT the datetime being
    converted (the recipe from the datetime docs): time.timezone/time.altzone
    give the zone's standard and DST offsets, and time.localtime decides which
    was in force. A zone with no DST answers one offset all year, so the object
    is a no-op there.

    A real IANA zone via TZ (below) is still better - it also gets historical
    rule changes right, which Windows does not - which is why the warning stays.
    """

    _DST_UNKNOWN = -1   #< tm_isdst's "let the platform decide" sentinel

    def _isDst(self, dt):
        if dt is None:
            return False
        try:
            stamp = _time.mktime((dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second,
                                  dt.weekday(), 0, self._DST_UNKNOWN))
            return _time.localtime(stamp).tm_isdst > 0
        except (OverflowError, OSError, ValueError):
            # Outside the platform's representable range (the year-1 and
            # far-future sentinels this codebase converts). Standard time is the
            # honest answer; raising here would break every such conversion.
            return False

    def utcoffset(self, dt):
        return datetime.timedelta(seconds=-(_time.altzone if self._isDst(dt) else _time.timezone))

    def dst(self, dt):
        if not self._isDst(dt):
            return datetime.timedelta(0)
        return datetime.timedelta(seconds=_time.timezone - _time.altzone)

    def tzname(self, dt):
        return _time.tzname[1 if self._isDst(dt) else 0]


# --- Instance timezone, chosen once at import ---------------------------------
tzName = environ.get("TZ")
tz = None

if not tzName:
    print("WARNING: TZ environment variable not set! Using system timezone.")
    print("         In Docker/containers, this is usually UTC. Set TZ explicitly -")
    print("         without it, historical daylight-saving rule changes can't be applied.")
    try:
        tz = _SystemLocalTimezone()
    except Exception:
        tz = datetime.timezone.utc
else:
    try:
        tz = ZoneInfo(tzName)
        print(f"Using timezone: {tzName}")
    except Exception as e:
        print(f"ERROR: Invalid timezone '{tzName}': {e}")
        print("       Falling back to UTC. Use a valid IANA timezone (e.g., 'America/Los_Angeles')")
        tz = datetime.timezone.utc
        tzName = None


# --- Error rendering -----------------------------------------------------------
def parseError(e) -> str:
    """A one-line account of an exception, naming the frame that raised it:
    "TypeError in file.py -> func() at line 12: 'the code' -> Error: message".

    Reads the exception's own __traceback__ (attached the moment it was
    raised), so it answers correctly for a stored exception object too, not
    only inside the except block that caught it."""
    frames = extract_tb(getattr(e, "__traceback__", None))
    if not frames:
        return f"{type(e).__name__}: {e}"

    frame = frames[-1]
    return (f"{type(e).__name__} in {basename(frame.filename)} -> {frame.name}() "
            f"at line {frame.lineno}: '{frame.line}' -> Error: {e}")


# --- Datetime helpers ----------------------------------------------------------
def _tzOrDefault(tz, default=None):
    """The tzinfo to actually use: `tz` when it really is one (a few call
    sites historically passed non-tzinfo values through **kwargs - anything
    else is ignored rather than crashing strftime later), otherwise `default`,
    otherwise the instance zone."""
    if isinstance(tz, datetime.tzinfo):
        return tz
    return default if default is not None else getTimezone()


def fromtimestamp(ts, tz=None) -> datetime.datetime:
    """The aware datetime for a Unix timestamp that may be negative (pre-1970)
    or far out of range. Windows' C runtime refuses negative timestamps (and
    raises for the far-out-of-range sentinels this codebase round-trips), so
    those are computed by offsetting from the epoch instead. Defaults to UTC,
    NOT the instance zone - timestamps are absolute."""
    tz = _tzOrDefault(tz, default=datetime.timezone.utc)
    with suppress(OSError, ValueError):
        return datetime.datetime.fromtimestamp(ts, tz)
    return datetime.datetime(1970, 1, 1, tzinfo=tz) + datetime.timedelta(seconds=ts)


def epoch(tz=None):
    """1970-01-01T00:00:00, the app's 'no date' placeholder value."""
    return fromtimestamp(0, tz=_tzOrDefault(tz))


def parseIsoDatetime(value) -> datetime.datetime:
    """fromisoformat, with the trailing "Z" Spotify's timestamps carry mapped
    to an explicit +00:00 offset."""
    return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def getTimezone() -> datetime.tzinfo:
    """The instance-wide zone (module-level, chosen once at import above)."""
    return tz   #< assigned once by the setup block above


def now(tz=None):
    return datetime.datetime.now(tz=_tzOrDefault(tz))


def toTimezone(dt: datetime.datetime, tz=None) -> datetime.datetime:
    """dt expressed in the given (or instance) zone. A naive dt is DECLARED to
    be in that zone - no shifting - because naive values in this codebase are
    local wall-clock readings, not disguised UTC."""
    tz = _tzOrDefault(tz)
    if dt.tzinfo is not None:
        return dt.astimezone(tz)
    return dt.replace(tzinfo=tz)


def startOfDay(dt: datetime.datetime = None, tz=None):
    """Midnight (in the given/instance zone) of the day containing dt (or now)."""
    dt = toTimezone(dt or now(tz=tz), tz=tz)
    return datetime.datetime.combine(dt.date(), datetime.time.min, tzinfo=dt.tzinfo)


def startOfWeek(dt: datetime.datetime = None, tz=None):
    """Monday 00:00 local time for the week containing dt (or now())."""
    dt = startOfDay(dt, tz=tz)
    return dt - datetime.timedelta(days=dt.weekday())


def startOfMonth(dt: datetime.datetime = None, tz=None):
    """The 1st of the month, 00:00 local time, for the month containing dt (or now())."""
    return startOfDay(dt, tz=tz).replace(day=1)


def parseDateString(dateText: str, tz=None):
    """A bare date ("2021-05-07", "2021-05", "2021") as an aware datetime at
    the start of its period, or None when no known form matches."""
    tz = _tzOrDefault(tz)
    text = str(dateText)
    for candidate in DATE_FORMATS:
        with suppress(ValueError):
            return datetime.datetime.strptime(text, candidate).replace(tzinfo=tz)
    return None   #< no known form matched


def parseDatetime(value, tz=None):
    """ISO form first, bare-date forms second; None when neither matches.

    The zone conversion stays INSIDE the guard: an in-range instant can still
    overflow when expressed in the target zone (year 9999 pushed east, year 1
    pushed west), and "cannot be read" has to cover that too - this is a
    fallback chain, not a parser."""
    with suppress(Exception):
        return toTimezone(parseIsoDatetime(value), tz)
    return parseDateString(value, tz=tz)


def convertToDatetime(timestamp, tz=None):
    """Anything this codebase stores as 'a time' - datetime, Unix timestamp,
    ISO string, bare date - as an aware datetime. The unparseable, and the
    "0000-00-00" placeholder old exports carry, map to the epoch rather than
    raising: one bad date on one row must not take down a whole page."""
    if isinstance(timestamp, datetime.datetime):   #< already a datetime: just normalize the zone
        return toTimezone(timestamp, tz=tz)

    # A bare four-digit string is a YEAR, and has to be read as one BEFORE the
    # numeric path below, because that path succeeds: float("1981") is 1981.0,
    # which is 33 minutes into 1970. Spotify sends exactly this - release_date
    # arrives at the precision Spotify knows the album to, so
    # release_date_precision "year" makes the field the four characters
    # "1981" - and both callers that convert it (Formatters/spotifyClient.py's
    # formatTrack and the album metadata backfiller) stored the 1970 value.
    # For the backfiller it stuck: getAlbumsMissingMetadata only re-queues
    # release_date = 0, so a wrong-but-nonzero date is never asked about again.
    #
    # In the shared helper rather than at the two call sites because the blast
    # radius is exactly "four digits": %Y matches nothing else, so "0000"
    # (no year 0), "12345" and "20210507" all stay numeric, and no caller can
    # mean "1000 to 9999 seconds after the epoch" by a four-character string.
    if isinstance(timestamp, str) and _BARE_YEAR_RE.fullmatch(timestamp):
        parsed = parseDateString(timestamp, tz=tz)
        if parsed is not None:
            return parsed

    # The CONVERSION sits inside the guard, not just the float() call: NaN and
    # infinity pass float() and then blow up in fromtimestamp's arithmetic,
    # and per the contract above they are "unparseable", not errors.
    with suppress(TypeError, ValueError, OverflowError):
        return fromtimestamp(float(timestamp), tz=tz)

    if timestamp == "0000-00-00":   #< the 'unknown date' placeholder old exports carry
        return epoch(tz=tz)

    parsed = parseDatetime(timestamp, tz=tz)
    return parsed if parsed is not None else epoch(tz=tz)


def dateToString(timestamp, tz=None):
    """The YYYY-MM-DD day, in the given (or instance) zone, of any value
    convertToDatetime accepts. The explicit toTimezone matters for numeric
    input: fromtimestamp answers in UTC, and the DAY a play belongs to is a
    local question."""
    return toTimezone(convertToDatetime(timestamp, tz=tz), tz=tz).strftime("%Y-%m-%d")

def listeningBuckets(rows):
    """The getBucketedPlayTotals rows that represent actual listening.

    Those rows stopped filtering is_skip=0 in the WHERE so a skip-only track's
    detail chart could render a timeline; a bucket whose plays were ALL skips
    now comes back with plays=0 instead of not coming back at all. Every caller
    that means "did this person listen" has to say so, because the mere
    EXISTENCE of a row no longer answers it - which is how a day holding one
    4-second skip started extending listening streaks and dating streak
    milestones, while the contribution calendar beside them (which has always
    tested count > 0) rendered that same day blank.

    Shared rather than retyped at each site: it is a one-line filter whose
    reasoning is the entire point, and there are three consumers in two modules
    (Database.database's streak/peak stats and services.milestones' date
    recalculation)."""
    return [row for row in rows if row["plays"]]

def timeToInt(timestampOrStr) -> int:
    """Unix seconds for a datetime, a numeric string, or any parseable time
    string; 0 when nothing matches (callers treat 0 as 'no timestamp'). A
    string with no zone marker is read in the instance zone - see timeToIntUTC
    for sources documented as UTC."""
    if isinstance(timestampOrStr, datetime.datetime):
        return int(toTimezone(timestampOrStr).timestamp())   #< toTimezone pins a zone on naive input first

    # int() inside the guard: "nan"/"inf" pass float() and fail int(), and the
    # contract is 0 for anything unreadable, not a ValueError mid-import.
    with suppress(TypeError, ValueError, OverflowError):
        return int(float(timestampOrStr))

    asDatetime = parseDatetime(timestampOrStr)
    return 0 if asDatetime is None else int(asDatetime.timestamp())

def timeToIntUTC(timestampOrStr):
    """Like timeToInt, but a date/time string with no timezone marker (no "Z" or
    offset) is interpreted as UTC rather than the app's local TZ - for sources
    that are documented as UTC but don't say so on the wire, e.g. Spotify's
    older Account-export "endTime" field."""
    try:
        value = str(timestampOrStr).replace("Z", "+00:00")
        parsed = datetime.datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return int(parsed.timestamp())
    except (ValueError, TypeError):
        return timeToInt(timestampOrStr)

def msToString(ms: int | float, hideSecondsAboveHours: int | None = None) -> str:
    """Converts milliseconds into a human-readable duration string.

    When `hideSecondsAboveHours` is set and the duration is at least that many
    hours, the seconds component is dropped (e.g. a 12h total reads "12h 3m"
    instead of "12h 3m 41s") - the seconds are noise at that scale. Left as None
    everywhere the precise value matters (now-playing progress, tooltips, ...).
    """
    if not ms or ms < 0:
        return "0s"

    hours, remainder = divmod(int(ms) // 1000, 3600)
    minutes, seconds = divmod(remainder, 60)

    labeled = []
    if hours:
        labeled.append(f"{hours}h")
    if minutes or hours:
        labeled.append(f"{minutes}m")
    showSeconds = hideSecondsAboveHours is None or hours < hideSecondsAboveHours
    if showSeconds and (seconds or minutes or hours):
        labeled.append(f"{seconds}s")
    # Under a full second every label above is skipped, so the join is "" - and
    # the guard at the top never sees these, since 500 is both truthy and
    # positive. Same answer as a literal zero rather than a blank cell.
    return " ".join(labeled) or "0s"


def formatDuration(durationMs: int) -> str:
    """m:ss - the player-style track length."""
    minutes, seconds = divmod(max(0, durationMs // 1000), 60)
    return f"{minutes}:{seconds:02d}"

SECONDS_PER_DAY = 86400
GAP_DAYS_PER_MONTH = 30    #< nominal month/year lengths for the timeline connectors' coarse
GAP_DAYS_PER_YEAR = 365    #  tiers - no calendar arithmetic, the label is approximate by design
# The months tier hands over at a full YEAR, not at 12 nominal months: 12*30 is
# 360 days, so days 360-364 used to satisfy neither `months < 12` nor `sec //
# (86400*365) >= 1` and rendered the literal "0 years later". Months are capped
# here instead of allowed to read "12 months later", which keeps the sequence
# monotone across the seam (359d and 364d both read "11 months later").
GAP_MAX_MONTHS_BELOW_A_YEAR = 11

def formatTimeGap(seconds: float | int) -> str:
    """Formats a time gap in seconds into a human-readable string for timeline connectors."""
    sec = max(0, int(seconds))
    if sec < 60:
        return "< 1 min later"
    
    minutes = sec // 60
    if minutes < 60:
        return f"{minutes} min later" if minutes == 1 else f"{minutes} mins later"
        
    hours = sec // 3600
    if hours < 24:
        return f"{hours} hour later" if hours == 1 else f"{hours} hours later"
        
    days = sec // SECONDS_PER_DAY
    if days < GAP_DAYS_PER_MONTH:
        return f"{days} day later" if days == 1 else f"{days} days later"

    if days < GAP_DAYS_PER_YEAR:
        months = min(sec // (SECONDS_PER_DAY * GAP_DAYS_PER_MONTH), GAP_MAX_MONTHS_BELOW_A_YEAR)
        return f"{months} month later" if months == 1 else f"{months} months later"

    years = sec // (SECONDS_PER_DAY * GAP_DAYS_PER_YEAR)
    return f"{years} year later" if years == 1 else f"{years} years later"

def versionTuple(version: str) -> tuple[int, ...]:
    """Version components as ints, so < and > order them correctly - a plain
    string compare puts "1.10.0" before "1.9.0"."""
    return tuple(int(component) for component in version.split("."))


if __name__ == "__main__":   #< `python Database/utils.py` drops into a REPL with the helpers loaded
    import code
    print("Try: timeToInt('2022-09-22T03:29:43Z'), then convertToDatetime(<that>)")
    code.interact(local=dict(globals(), **locals()))
