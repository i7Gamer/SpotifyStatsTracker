"""Stateless service logic extracted from app.py (behavior-preserving).

Each module here holds pure(ish) helpers that a route or template needs but that
carry no Flask request/app state - so they can be unit-tested and reasoned about
without standing up the whole SpotifyDashboardApp. app.py re-exports the public
names it (and the test suite) still import by name.
"""
