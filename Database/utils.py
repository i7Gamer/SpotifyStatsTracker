import os
import sys
import traceback
import datetime

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

def convertToDatetime(timestamp):
    try:
        playedAt = datetime.datetime.fromtimestamp(float(timestamp), datetime.timezone.utc)
    except (ValueError, TypeError):
        try:
            dt = datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                playedAt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
            else:
                playedAt = dt
        except Exception:
            if timestamp == "0000-00-00":
                return datetime.datetime.fromtimestamp(0, datetime.timezone.utc)     #< 1970 in unix time
            return datetime.datetime.strptime(timestamp, "%Y-%m-%d")

    return playedAt

def dateToString(timestamp):
    if type(timestamp) == float:
        timestamp = datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc)

    return timestamp.strftime("%Y-%m-%d")

def timeToInt(timestampOrStr):
    """ Convert ISO string or datetime to int """
    try:
        return int(float(timestampOrStr))
    except (ValueError, TypeError):
        try:
            dt = datetime.datetime.fromisoformat(timestampOrStr.replace("Z", "+00:00"))
            return int(dt.timestamp())
        except:
            return 0

def msToString(ms: int | float) -> str:
    """ Converts milliseconds into a human-readable duration string. """
    if ms is None or ms <= 0:
        return "0ms"

    totalSeconds = int(ms) // 1000

    seconds = totalSeconds % 60
    minutes = (totalSeconds // 60) % 60
    hours = totalSeconds // 3600
    
    parts = []

    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or hours > 0:
        parts.append(f"{minutes}m")
    if seconds > 0 or minutes > 0 or hours > 0:
        parts.append(f"{seconds}s")
        
    return " ".join(parts)

def formatDuration(ms: int) -> str:
    seconds = max(0, ms // 1000)
    minutes = seconds // 60
    remaining = seconds % 60
    return f"{minutes}:{remaining:02d}"

def versionTuple(version: str) -> tuple:
    """ Can be used to compare versions with > and < """
    return tuple(int(x) for x in version.split("."))

if __name__ == "__main__":
    import pysole
    pysole.probe(runRemainingCode=True)
    print("un = timeToInt('2022-09-22T03:29:43Z')")
    print("dt = convertToDatetime(un)")