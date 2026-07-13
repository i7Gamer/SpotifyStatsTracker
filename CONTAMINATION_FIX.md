# Cross-User Data Contamination Fix

## Problem
A critical bug was discovered where plays from one user could be recorded under another user's account. Both users had identical play data (same timestamp 2026-07-10 23:21:25, same duration 295569ms) for "13 Beaches", indicating corrupted or shared Spotify session state between listeners.

## Root Cause
When `spotapi.Spotify()` instances are created for different users, there may be global session caching or shared state that causes `current_user_recently_played()` to return mixed data from multiple users' sessions. This contaminated data then gets recorded under the wrong user's account.

## Solution Implemented

### 1. Listener Session Validation (spotifyListener.py)

**Before the poll loop:**
- Store the authenticated user's Spotify ID on listener initialization
- Validate session identity before each poll
- Log error and trigger reconnection if session changes

**Log entries:**
```
INFO Listener initialized for user timorzipa (Spotify ID: abc123)
ERROR Session user mismatch! Expected abc123, got xyz789 - this could indicate cross-user contamination
```

### 2. Callback Logging (spotifyListener.py)

**When new plays are received:**
- Log the count of new items and which user they belong to
- Timestamps at the callback invocation point

**Log entries:**
```
INFO Listener callback: 42 new items for user timorzipa
```

### 3. Contamination Detection (database.py)

**Timestamp validation in _addToDatabaseFromListener():**
- Check if any play has a timestamp more than 1 day in the future
- This is impossible in normal operation (Spotify's API returns plays up to "now")
- If found, log a critical error and skip the suspicious play
- Prevents corrupted data from being permanently recorded

**Play recording logging:**
- Log every play as it's recorded with user, track ID, name, timestamp, and duration
- Allows post-hoc audit trail to detect contamination patterns

**Log entries:**
```
INFO Recording play for user timorzipa: track=6VXpPeE3e5KGVQJuYednVS (13 Beaches), timestamp=1783718485, duration=295569ms
ERROR CONTAMINATION CHECK FAILED: Track abc123 has timestamp 9999999999 (2 days in future). This suggests cross-user data contamination. Skipping this play.
```

## Monitoring

**Watch for these patterns in app.log:**

1. **Session validation failures** → indicates Spotify object session mixing
   ```
   Session user mismatch! Expected XYZ, got ABC
   ```

2. **Future timestamp rejections** → indicates contamination attempt detected
   ```
   CONTAMINATION CHECK FAILED: ... timestamp ... in future
   ```

3. **Listener reconnection loops** → suggests underlying spotapi/session issue
   ```
   Listener session validation failed - triggering reconnection
   ```

## Location of Changes

- **Database/Listeners/spotifyListener.py**: Added `_authenticated_user_id`, `_validateCurrentUser()`, session validation in `_checkOnce()`
- **Database/database.py**: Added timestamp/future-date validation in `_addToDatabaseFromListener()`, enhanced logging in `appendTrackData()`
- **Database/Data/app.log**: All protective logs written here

## Log File Location
`Database/Data/app.log` (rotates at 5MB, keeps 3 backup files)

## Future: Point 4
When needed, add REST API verification to query Spotify's API to confirm a play actually belongs to the current user before recording (defensive API call). This is not yet implemented as it adds latency and API rate-limit risk.
