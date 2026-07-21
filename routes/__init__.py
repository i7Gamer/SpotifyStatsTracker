"""Per-domain HTTP route modules, split out of app.py's registerRoutes.

Each module exposes ``register(app, dashboard)``, which defines that domain's
view functions as closures over the SpotifyDashboardApp instance and registers
them via ``app.add_url_rule`` under their ORIGINAL endpoint names - so every
existing ``url_for(...)`` in templates and Python keeps resolving unchanged (no
Flask-Blueprint endpoint namespacing). SpotifyDashboardApp.registerRoutes calls
each module's register().
"""
