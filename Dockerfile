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

# Copy application code
COPY . .

# Drop root. docker-compose bind-mounts ./Database/Data and ./autoImport from
# the host, so everything the app writes - the database, backups, the cover-art
# cache, app.log - landed root-owned in the user's own working tree, and any RCE
# in Flask/spotapi/websockets was root with write access to those mounts.
#
# A fixed uid (not just a name) so the host-side ownership of those mounts is
# predictable across rebuilds. The volumes are chowned rather than the whole
# tree: the code is read-only at runtime and copying it twice would double the
# image's largest layer.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/Database/Data /app/autoImport /app/secrets \
    && chown -R app:app /app/Database/Data /app/autoImport /app/secrets
USER app

# Expose Flask port
EXPOSE 5000

# Set environment variables
ENV FLASK_APP=wsgi.py
ENV PYTHONUNBUFFERED=1

# Backed by GET /health (checks DB connectivity, not just process liveness) -
# uses stdlib urllib so no extra package (curl/wget) is needed in this slim
# image just for the check itself.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:5000/health', timeout=3).getcode() == 200 else 1)"

# Run the Flask app
CMD ["python", "wsgi.py"]