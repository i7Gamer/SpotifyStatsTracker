"""Reading the /charts fragment the way the browser does.

The page is eight canvases, so its second request answers with the card's
MARKUP (headings, the range-scoped Top Genres section, the sections a range
actually has) plus one JSON data island holding every series - see
templates/_charts_results.html. htmx swaps the markup; static/js/charts-page.js
parses the island into window.__chartData and redraws.

`chartData(resp)` is that parse, so a test asserting on a dataset reads the same
object the page does. Imported by bare module name like the suite's other shared
helpers, since tests/ is on sys.path with no package __init__.
"""
import json

#< what htmx puts on every request it makes, plus the id of the region it fills
HX_HEADERS = {"HX-Request": "true", "HX-Target": "chartsCard"}

_ISLAND_OPEN = 'id="chartsData">'


def chartData(resp):
    """window.__chartData as the page will build it from `resp`."""
    body = resp.get_data(as_text=True)
    start = body.index(_ISLAND_OPEN) + len(_ISLAND_OPEN)
    return json.loads(body[start:body.index("</script>", start)])
