FROM python:3.13-slim

WORKDIR /app

# Copy requirements FIRST so the RUN command can use it
COPY requirements.txt .

# Install system dependencies, install python dependencies, then remove bloat.
# git is needed only because requirements.txt pins spotapi to a commit of
# TzurSoffer's fork (see the note there) - it used to be uninstalled and
# reinstalled from the fork's unpinned HEAD right here, which made every build
# non-reproducible and meant CI never tested what this image actually runs.
#
# `apt-get upgrade` is a deliberate exception to that reproducibility goal, not
# an oversight: the base image lags its own security updates, and two builds of
# the same tag differing by a patched libssl is the trade that is worth making.
# Everything the app's behaviour depends on - Python packages, spotapi's commit -
# is pinned, so this can only move OS-level packages.
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends gcc git \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y git gcc \
    && apt-get autoremove --purge -y \
    && rm -rf /var/lib/apt/lists/*

# The application itself (everything .dockerignore lets through)
COPY . .

# Deliberately still runs as root, and that is a considered choice rather than an
# oversight. docker-compose bind-mounts ./Database/Data and ./autoImport, so a
# non-root uid cannot write to them until the operator chowns those directories -
# which silently breaks every existing deployment on upgrade. Weighed against a
# threat model where the container is the only process on a single-tenant host and
# already holds the database it would be attacked for, the upgrade break costs
# more than the isolation buys. Revisit if this ever runs somewhere multi-tenant.

# The port waitress serves on inside the container
EXPOSE 5000

# Baseline environment; compose overrides/extends these
ENV FLASK_APP=wsgi.py
ENV PYTHONUNBUFFERED=1

# Backed by GET /health (checks DB connectivity, not just process liveness) -
# uses stdlib urllib so no extra package (curl/wget) is needed in this slim
# image just for the check itself.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:5000/health', timeout=3).getcode() == 200 else 1)"

# Serve via wsgi.py (waitress), not the Flask dev server
CMD ["python", "wsgi.py"]