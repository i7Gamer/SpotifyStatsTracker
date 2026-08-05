"""Reading /compare's htmx refresh back region by region.

The Compare page's second request used to answer ``?ajax=true`` with a dict of
named HTML chunks; it now answers the ``HX-Request`` header with ONE HTML
fragment whose elements each name the region they replace (``hx-swap-oob``).
The tests that assert on the page's content are about one region at a time, and
still are - so this slices the fragment back apart under the payload keys they
were written against, rather than rewriting a hundred assertions to say the same
thing in a new spelling.

The transport itself (which regions exist, the narrow ?scope=sortable refresh,
HX-Redirect) is pinned by tests/test_compare_htmx.py, not here.

Imported by bare module name (``from _compare_fragment import ...``) like the
suite's other shared helpers, since tests/ is on sys.path with no package
__init__.
"""
import json
import re

#< what htmx puts on every request it makes; the marker that was ?ajax=true
HX_HEADERS = {"HX-Request": "true"}

# {old payload key: the id of the region that content now lives in}. Only the
# regions whose value is HTML - the three derived ones are read separately
# below, because the fragment states them as markup rather than as values.
FRAGMENT_REGIONS = {
    "statsTableHtml": "compareStatsTable",
    "similaritiesHtml": "compareSimilarities",
    "genresHtml": "compareGenres",
    "sharedArtistsHtml": "sharedArtistsList",
    "sharedSongsHtml": "sharedSongsList",
    "sharedAlbumsHtml": "sharedAlbumsList",
    "myTopSongsHtml": "myTopSongsList",
    "theirTopSongsHtml": "theirTopSongsList",
    "myTopArtistsHtml": "myTopArtistsList",
    "theirTopArtistsHtml": "theirTopArtistsList",
    "myTopAlbumsHtml": "myTopAlbumsList",
    "theirTopAlbumsHtml": "theirTopAlbumsList",
}


def innerHtml(fragment, elementId):
    """The inner HTML of the fragment's element with this id, or None.

    A raw substring, not a parse-and-reserialize: several callers assert on
    exact rendered text (attribute quoting, ``&#39;`` entities, the space a
    ``{% if %}`` leaves behind), all of which a round trip through an HTML
    parser would quietly normalize into something else.
    """
    opener = re.search(r'<(?P<tag>[a-z]+)(?=[\s>])[^>]*\bid="%s"' % re.escape(elementId),
                       fragment)
    if opener is None:
        return None
    tag = opener.group("tag")
    bodyStart = fragment.index(">", opener.end()) + 1
    #< the regions nest same-named elements (a <div> region full of card
    #  <div>s), so the close has to be counted to rather than searched for
    boundary = re.compile(r"<(/?)%s(?=[\s/>])" % re.escape(tag))
    depth = 1
    position = bodyStart
    while True:
        match = boundary.search(fragment, position)
        if match is None:
            raise AssertionError(f"unclosed <{tag} id={elementId}> in the compare fragment")
        depth += -1 if match.group(1) else 1
        if depth == 0:
            return fragment[bodyStart:match.start()]
        position = match.end()


def tasteMatchScore(fragment):
    """The taste match as the old payload carried it: an int, or None when
    there is no score. The fragment says the same thing in markup - a hidden
    badge means "no score", which is not the same claim as 0%."""
    badge = re.search(r'<div[^>]*\bid="tasteMatch"[^>]*>', fragment)
    if badge is None or "display: none" in badge.group(0):
        return None
    return int(re.search(r'js-taste-match">(\d+)%', fragment).group(1))


def selectedCounterpart(fragment):
    """Which counterpart the refresh actually rendered - the authorization
    answer, read off the hidden field that carries ?with= into the next
    request."""
    field = re.search(r'<input[^>]*\bid="compareWithField"[^>]*\bvalue="([^"]*)"', fragment)
    return field.group(1) if field else None


def counterpartLabel(fragment):
    """The name the counterpart is SHOWN under. It used to be a payload field
    the loader wrote into every .js-with-username element by hand; the fragment
    now carries one element that addresses them all by class, so the label is
    that element's content."""
    label = re.search(r'<span hx-swap-oob="innerHTML:\.js-with-username">([^<]*)</span>', fragment)
    return label.group(1) if label else None


def fragmentRegions(fragment):
    """Every region of the refresh, keyed the way the old JSON payload was.

    A region the response did not carry is None, which is what the narrow
    ?scope=sortable refresh is tested with: it leaves twelve of these unset on
    purpose.
    """
    regions = {key: innerHtml(fragment, regionId)
               for key, regionId in FRAGMENT_REGIONS.items()}
    regions["withUsername"] = selectedCounterpart(fragment)
    regions["withDisplayName"] = counterpartLabel(fragment)
    regions["tasteMatch"] = tasteMatchScore(fragment)
    trend = innerHtml(fragment, "compareTrendData")
    regions["comparisonTrend"] = json.loads(trend) if trend is not None else None
    return regions


def fetchComparison(client, url):
    """(response, regions) for the refresh htmx makes on first paint and on
    every filter change."""
    resp = client.get(url, headers=HX_HEADERS)
    return resp, fragmentRegions(resp.get_data(as_text=True))
