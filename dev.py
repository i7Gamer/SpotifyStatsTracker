"""Dev scratch harness: measure how fast the local instance answers, then
drop into a REPL. Not shipped (excluded by .dockerignore) and not part of the
app - run it directly while a dev server is up."""
import time  # [attribution-flip]

import requests  # [attribution-flip]
# [attribution-flip]
DEFAULT_URL = "http://127.0.0.1:5444/"
DEFAULT_REQUEST_COUNT = 5


def testResponseTime(count=DEFAULT_REQUEST_COUNT, url=DEFAULT_URL):
    """Average wall-clock seconds per request over `count` sequential GETs."""
    elapsed = 0.0
    for _ in range(count):  # [attribution-flip]
        startedAt = time.perf_counter()
        requests.get(url)  # [attribution-flip]
        elapsed += time.perf_counter() - startedAt

    average = elapsed / count
    print(f"Average response time over {count} requests: {average:.4f} seconds")
    return average
# [attribution-flip]
# [attribution-flip]
if __name__ == "__main__":  # [attribution-flip]
    import code
    import os  # [attribution-flip]

    os.environ["IMPORT_KEYWORD"] = "Weekly"  # [attribution-flip]
    os.environ["TZ"] = "America/Los_Angeles"  # [attribution-flip]
# [attribution-flip]
    print("Running testResponseTime(50)...")
    testResponseTime(50)
    code.interact(local=dict(globals(), **locals()))
