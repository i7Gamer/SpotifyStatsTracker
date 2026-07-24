import os
import sys
import traceback
import datetime
from zoneinfo import ZoneInfo

DATE_FORMATS = ("%Y-%m-%d", "%Y-%m", "%Y")

## TIMEZONE SETUP
tzName = os.environ.get("TZ")
tz = None

if not tzName:
    print("WARNING: TZ environment variable not set! Using system timezone.")
    print("         In Docker/containers, this is usually UTC. Set TZ explicitly.")
    try:
        tz = datetime.datetime.now().astimezone().tzinfo
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


## ERROR PRINTING
def parseError(e):
    _, _, excTb = sys.exc_info()
    summary = traceback.extract_tb(excTb)

    if summary:
        lastFrame = summary[-1]
        fname = os.path.basename(lastFrame.filename)
        lineno = lastFrame.lineno
        funcName = lastFrame.name
        codeLine = lastFrame.line

        return f"{type(e).__name__} in {fname} -> {funcName}() at line {lineno}: '{codeLine}' -> Error: {e}"

    return f"{type(e).__name__}: {e}"


## DATETIME RELATED
def fromtimestamp(ts, tz=None):
    """
    Cross-platform safe timestamp conversion.
    Windows cannot handle negative timestamps, so we manually offset from epoch.
    """
    if tz is not None and not isinstance(tz, datetime.tzinfo):
        tz = None
    if tz is None:
        tz = datetime.timezone.utc

    try:
        # Works on Linux/macOS
        return datetime.datetime.fromtimestamp(ts, tz=tz)
    except (OSError, ValueError):
        # Windows fallback for negative timestamps
        epoch = datetime.datetime(1970, 1, 1, tzinfo=tz)
        return epoch + datetime.timedelta(seconds=ts)

def epoch(tz=None):
    if tz is not None and not isinstance(tz, datetime.tzinfo):
        tz = None
    if tz is None:
        tz = getTimezone()
    return fromtimestamp(0, tz=tz)

def parseIsoDatetime(value):
    """
    Handles ISO strings, including those ending with Z.
    """
    value = str(value).replace("Z", "+00:00")
    return datetime.datetime.fromisoformat(value)

def getTimezone():
    return tz

def now(tz=None):
    if tz is not None and not isinstance(tz, datetime.tzinfo):
        tz = None
    if tz is None:
        tz = getTimezone()
    return datetime.datetime.now(tz=tz)

def toTimezone(dt: datetime.datetime, tz=None):
    if tz is not None and not isinstance(tz, datetime.tzinfo):
        tz = None
    if tz is None:
        tz = getTimezone()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def startOfDay(dt: datetime.datetime = None, tz=None):
    dt = toTimezone(dt or now(tz=tz), tz=tz)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def startOfWeek(dt: datetime.datetime = None, tz=None):
    """Monday 00:00 local time for the week containing dt (or now())."""
    dt = startOfDay(dt, tz=tz)
    return dt - datetime.timedelta(days=dt.weekday())


def startOfMonth(dt: datetime.datetime = None, tz=None):
    """The 1st of the month, 00:00 local time, for the month containing dt (or now())."""
    return startOfDay(dt, tz=tz).replace(day=1)


def parseDateString(dateText: str, tz=None):
    if tz is not None and not isinstance(tz, datetime.tzinfo):
        tz = None
    if tz is None:
        tz = getTimezone()
    for fmt in DATE_FORMATS:
        try:
            return datetime.datetime.strptime(str(dateText), fmt).replace(tzinfo=tz)
        except ValueError:
            pass
    return None

def parseDatetime(value, tz=None):
    try:
        return toTimezone(parseIsoDatetime(value), tz)
    except Exception:
        return parseDateString(value, tz=tz)

def convertToDatetime(timestamp, tz=None):
    """
    Converts:
    - datetime -> normalized
    - numeric timestamp -> safe conversion
    - ISO string -> parsed
    - date-only string -> parsed
    - "0000-00-00" -> epoch
    - invalid -> epoch
    """
    if isinstance(timestamp, datetime.datetime):
        return toTimezone(timestamp, tz=tz)

    try:
        return fromtimestamp(float(timestamp), tz=tz)
    except (ValueError, TypeError):
        pass

    if timestamp == "0000-00-00":
        return epoch(tz=tz)

    parsed = parseDatetime(timestamp, tz=tz)
    return parsed if parsed is not None else epoch(tz=tz)

def dateToString(timestamp, tz=None):
    if type(timestamp) in (float, int):
        timestamp = fromtimestamp(timestamp, tz=tz)
    elif type(timestamp) != datetime.datetime:
        timestamp = convertToDatetime(timestamp, tz=tz)

    timestamp = toTimezone(timestamp, tz=tz)
    return timestamp.strftime("%Y-%m-%d")

def timeToInt(timestampOrStr):
    """
    Converts datetime or string to integer timestamp.
    Handles negative timestamps safely.
    """
    if type(timestampOrStr) == datetime.datetime:
        return int(toTimezone(timestampOrStr).timestamp())

    try:
        return int(float(timestampOrStr))
    except (ValueError, TypeError):
        pass

    parsed = parseDatetime(timestampOrStr)
    return int(parsed.timestamp()) if parsed else 0

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
    if ms is None or ms <= 0:
        return "0s"

    totalSeconds = int(ms) // 1000

    seconds = totalSeconds % 60
    minutes = (totalSeconds // 60) % 60
    hours = totalSeconds // 3600

    parts = []

    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or hours > 0:
        parts.append(f"{minutes}m")
    showSeconds = hideSecondsAboveHours is None or hours < hideSecondsAboveHours
    if showSeconds and (seconds > 0 or minutes > 0 or hours > 0):
        parts.append(f"{seconds}s")

    return " ".join(parts)

def formatDuration(ms: int) -> str:
    seconds = max(0, ms // 1000)
    minutes = seconds // 60
    remaining = seconds % 60
    return f"{minutes}:{remaining:02d}"

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
        
    days = sec // 86400
    if days < 30:
        return f"{days} day later" if days == 1 else f"{days} days later"
        
    months = sec // (86400 * 30)
    if months < 12:
        return f"{months} month later" if months == 1 else f"{months} months later"
        
    years = sec // (86400 * 365)
    return f"{years} year later" if years == 1 else f"{years} years later"

def versionTuple(version: str) -> tuple:
    """ Can be used to compare versions with > and < """
    return tuple(int(x) for x in version.split("."))


if __name__ == "__main__":
    import code
    print("un = timeToInt('2022-09-22T03:29:43Z')")
    print("dt = convertToDatetime(un)")
    code.interact(local=dict(globals(), **locals()))
