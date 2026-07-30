"""Dev scratch harness: measure how fast the local instance answers, then
drop into a REPL. Not shipped (excluded by .dockerignore) and not part of the
app - run it directly while a dev server is up."""
import time

import requests

DEFAULT_URL = "http://127.0.0.1:5444/"
DEFAULT_REQUEST_COUNT = 5


def testResponseTime(count=DEFAULT_REQUEST_COUNT, url=DEFAULT_URL):
    """Average wall-clock seconds per request over `count` sequential GETs."""
    elapsed = 0.0
    for _ in range(count):
        startedAt = time.perf_counter()
        requests.get(url)
        elapsed += time.perf_counter() - startedAt

    average = elapsed / count
    print(f"Average response time over {count} requests: {average:.4f} seconds")
    return average


if __name__ == "__main__":
    import code
    import os

    os.environ["IMPORT_KEYWORD"] = "Weekly"
    os.environ["TZ"] = "America/Los_Angeles"

    print("Running testResponseTime(50)...")
    testResponseTime(50)
    code.interact(local=dict(globals(), **locals()))
