# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Exercise the owned Spotify client against the REAL Spotify, and check that
what comes back is actually populated.

Why this exists: every test in the suite is mocked, and the formatters in
Database/Spotify/formatting.py were written from the pathfinder GraphQL wire
shapes rather than from captured traffic. If Spotify nests a field differently
than assumed - `coverArt.sources`, `tracksV2.items[].track`, `visuals.avatarImage`
- the mocked tests still pass while production quietly degrades into fallback
records, blank artwork and zero durations. Only a real call can catch that, so
this checks values are POPULATED, not merely present.

It is also useful operationally: run it inside a container to answer "is our
Spotify client still working, or is it the credentials?" - and after a TOTP
rotation it tells you which of the two you are looking at.

    python -m Database.Spotify.smoketest <sessionFile> [email]

`sessionFile` is a spotapi sessions file - see session.example.json beside this
module for the format and for how to obtain the cookie values. `email` picks one
session out of a file holding several; omitted, the first entry is used.

Most of what this checks does NOT need working credentials: search, track,
album, artist and playlist are public endpoints, so running it against the
unedited session.example.json still validates every formatter. Only
current_user() proves the cookies themselves - so a run where just that fails
means "the shapes are fine, the credentials aren't".

Exit code is 0 when every required check passed, 1 otherwise. Nothing is
written and nothing is recorded - it only reads.
"""

import json
import sys
import traceback

from Database.Spotify import Spotify
from Database.utils import convertToDatetime

# Sung by a lot of people, in every market, and old enough to be stable. Only
# the SEARCH term is hardcoded - the track/album/artist ids come from whatever
# it returns, so this can't rot the way pinned ids would.
SEARCH_QUERY = "track:Bohemian Rhapsody artist:Queen"

# Editorial playlists are increasingly restricted for non-official clients, and
# the app only ever reads .get("name") off a playlist (falling back to "Unknown
# Playlist"), so a failure here is reported but does not fail the run.
OPTIONAL_PLAYLIST_ID = "37i9dQZF1DXcBWIGoYBM5M"

PREVIEW_LEN = 400   #< how much of a payload to show when a check fails


class Report:
    """Collects check results so one failure doesn't stop the rest - a run that
    stopped at the first problem would hide the other three."""

    def __init__(self):
        self.failures = []
        self.warnings = []

    def check(self, label, ok, detail=""):
        if ok:
            print(f"  PASS  {label}")
            return True
        print(f"  FAIL  {label}" + (f"  <- {detail}" if detail else ""))
        self.failures.append(label)
        return False

    def warn(self, label, detail=""):
        print(f"  WARN  {label}" + (f"  <- {detail}" if detail else ""))
        self.warnings.append(label)

    def note(self, message):
        print(f"        {message}")


def _preview(value) -> str:
    """A short, readable slice of whatever came back - the whole point when a
    shape assumption turns out to be wrong."""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = repr(value)
    return text[:PREVIEW_LEN] + ("..." if len(text) > PREVIEW_LEN else "")


def _checkTrackShape(report, track, source):
    """Every field Database/Formatters/spotifyClient.py reads off a track, with
    the two it hard-subscripts (id, external_urls.spotify) first."""
    report.check(f"{source}: has a real id", bool(track.get("id")), _preview(track))
    report.check(f"{source}: has a Spotify url",
                 bool((track.get("external_urls") or {}).get("spotify")),
                 _preview(track.get("external_urls")))
    report.check(f"{source}: has a name", bool(track.get("name")), _preview(track.get("name")))
    report.check(f"{source}: duration_ms is non-zero", (track.get("duration_ms") or 0) > 0,
                 f"got {track.get('duration_ms')!r} - formatting.py reads duration.totalMilliseconds")

    artists = track.get("artists") or []
    if report.check(f"{source}: has artists", bool(artists), _preview(track.get("artists"))):
        first = artists[0]
        report.check(f"{source}: artist has id + name", bool(first.get("id") and first.get("name")),
                     _preview(first))

    album = track.get("album") or {}
    report.check(f"{source}: album has id + name", bool(album.get("id") and album.get("name")),
                 _preview(album))

    images = album.get("images") or []
    if report.check(f"{source}: album has cover art", bool(images), _preview(album.get("images"))):
        widths = [image.get("width") or 0 for image in images]
        report.check(f"{source}: cover art is largest-first", widths == sorted(widths, reverse=True),
                     f"widths {widths} - Client.formatTrack takes images[0] as THE artwork")
        report.check(f"{source}: cover art has a url", bool(images[0].get("url")), _preview(images[0]))

    report.check(f"{source}: explicit is a bool", isinstance(track.get("explicit"), bool),
                 f"got {type(track.get('explicit')).__name__}")


def run(sessionFile, email=None) -> int:
    report = Report()

    print(f"\nSession file: {sessionFile}")
    print(f"Email:        {email or '(first entry in the file)'}\n")

    print("login")
    try:
        sp = Spotify(cookiesFile=sessionFile, email=email)
    except Exception as e:
        report.check("client constructed", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()
        return 1

    # Deliberately not treated as proof of anything: spotapi's
    # Login.from_cookies sets logged_in = True without asking Spotify, so
    # isLoggedIn() only says a session object was built. current_user() below
    # is the check that actually exercises the credentials - which is why the
    # run continues past a failure here rather than stopping.
    if not report.check("session object built (isLoggedIn)", sp.isLoggedIn(),
                        "the session file is missing or malformed - check the JSON shape "
                        "against session.example.json"):
        return 1

    print("\ncurrent_user  (the only check that needs real credentials)")
    try:
        user = sp.current_user()
        report.check("profile has an id", bool(user.get("id")), _preview(user))
        report.check("profile has an email", bool(user.get("email")), _preview(user))
        report.note(f"authenticated as {user.get('email') or user.get('id')}")
    except Exception as e:
        report.check("current_user() succeeded", False, f"{type(e).__name__}: {e}")
        report.note("cookies are missing/expired/placeholder - the public checks below "
                    "still validate every formatter, so read on")

    print("\nsearch")
    trackId = albumId = artistId = None
    try:
        items = (sp.search(SEARCH_QUERY) or {}).get("tracks", {}).get("items") or []
        if report.check("search returned tracks", bool(items), f"query was {SEARCH_QUERY!r}"):
            _checkTrackShape(report, items[0], "search")
            trackId = items[0].get("id")
            albumId = (items[0].get("album") or {}).get("id")
            artistId = ((items[0].get("artists") or [{}])[0]).get("id")
            report.note(f"derived ids - track={trackId} album={albumId} artist={artistId}")
    except Exception as e:
        report.check("search() succeeded", False, f"{type(e).__name__}: {e}")

    print("\ntrack")
    if trackId:
        try:
            track = sp.track(trackId)
            _checkTrackShape(report, track, "track")
            # A fallback record means Spotify answered without a usable track;
            # the play would still be recorded, but with no real metadata.
            report.check("track is real metadata, not a fallback record",
                         track.get("created_reason") is None,
                         f"created_reason={track.get('created_reason')!r} - see fallbackTrackRecord")
        except Exception as e:
            report.check("track() succeeded", False, f"{type(e).__name__}: {e}")
    else:
        report.warn("track skipped", "no id could be derived from search")

    print("\ntrack (uri form)")
    if trackId:
        try:
            # The importer passes spotify:track: URIs straight from export files;
            # the wrapper this client replaced mangled them into a doomed lookup.
            byUri = sp.track(f"spotify:track:{trackId}")
            report.check("a spotify:track: URI resolves to the same track",
                         byUri.get("id") == trackId,
                         f"got {byUri.get('id')!r}, expected {trackId!r}")
        except Exception as e:
            report.check("track(uri) succeeded", False, f"{type(e).__name__}: {e}")

    print("\nalbum")
    if albumId:
        try:
            album = sp.album(albumId)
            report.check("album has a name", bool(album.get("name")), _preview(album))
            report.check("album total_tracks is non-zero", (album.get("total_tracks") or 0) > 0,
                         f"got {album.get('total_tracks')!r} - reads tracksV2.totalCount")
            releaseDate = album.get("release_date")
            if report.check("album has a release_date", bool(releaseDate), _preview(album)):
                year = convertToDatetime(releaseDate).year
                report.check("release_date parses to a sane year", 1900 < year < 2100,
                             f"parsed {releaseDate!r} -> {year}")
            # The metadata backfiller's ONLY duration source for tracks whose own
            # lookup came back blanked.
            tracks = (album.get("tracks") or {}).get("items") or []
            if report.check("album carries its track list", bool(tracks), _preview(album.get("tracks"))):
                report.check("album track has id + name + duration",
                             bool(tracks[0].get("id") and tracks[0].get("name"))
                             and (tracks[0].get("duration_ms") or 0) > 0,
                             _preview(tracks[0]))
        except Exception as e:
            report.check("album() succeeded", False, f"{type(e).__name__}: {e}")
    else:
        report.warn("album skipped", "no id could be derived from search")

    print("\nartist")
    if artistId:
        try:
            artist = sp.artist(artistId)
            report.check("artist has a name", bool(artist.get("name")), _preview(artist))
            images = artist.get("images") or []
            # media_fetch reads images[0]["url"]; an empty list is a valid "no
            # artwork", but for a major artist it means the shape guess is wrong.
            if report.check("artist has images", bool(images),
                            "empty for a well-known artist suggests visuals.avatarImage moved"):
                widths = [image.get("width") or 0 for image in images]
                report.check("artist images are largest-first", widths == sorted(widths, reverse=True),
                             f"widths {widths}")
                report.check("artist image has a url", bool(images[0].get("url")), _preview(images[0]))
        except Exception as e:
            report.check("artist() succeeded", False, f"{type(e).__name__}: {e}")
    else:
        report.warn("artist skipped", "no id could be derived from search")

    print("\nplaylist (optional)")
    try:
        playlist = sp.playlist(OPTIONAL_PLAYLIST_ID)
        if playlist.get("name"):
            print(f"  PASS  playlist has a name ({playlist['name']!r})")
        else:
            report.warn("playlist returned no name",
                        "the listener falls back to 'Unknown Playlist'; editorial "
                        "playlists are often restricted for non-official clients")
    except Exception as e:
        report.warn("playlist() raised", f"{type(e).__name__}: {e} (non-fatal, see above)")

    print("\nrecently-played buffer")
    recent = sp.current_user_recently_played()
    # A LOCAL buffer, not a Spotify call - empty is correct for a fresh client.
    report.check("returns a list", isinstance(recent, list), f"got {type(recent).__name__}")
    report.note(f"{len(recent)} buffered play(s) - empty is expected without a running listener")

    print("\n" + "-" * 68)
    if report.warnings:
        print(f"{len(report.warnings)} warning(s): {', '.join(report.warnings)}")
    if report.failures:
        print(f"FAILED - {len(report.failures)} check(s): {', '.join(report.failures)}")
        if report.failures == ["current_user() succeeded"]:
            print("Only the credential check failed: every formatter validated against real\n"
                  "Spotify responses. Supply working cookies to verify the authenticated path.")
        else:
            print("A shape failure means Database/Spotify/formatting.py needs updating for what\n"
                  "Spotify actually returns. An auth-wide failure means the cookies or the pinned\n"
                  "TOTP secret (SPOTIFY_TOTP_SECRET_VERSION in Database/patches.py) need attention.")
        return 1
    print("OK - every required check passed.")
    return 0


def main(argv) -> int:
    if not 2 <= len(argv) <= 3:
        print(__doc__.strip())
        return 2
    return run(argv[1], argv[2] if len(argv) == 3 else None)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
