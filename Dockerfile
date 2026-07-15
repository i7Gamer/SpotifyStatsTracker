FROM python:3.13-slim

WORKDIR /app

# Copy requirements FIRST so the RUN command can use it
COPY requirements.txt .

# Install system dependencies, install python dependencies, install custom SpotAPI, then remove bloat
RUN apt-get update && apt-get install -y --no-install-recommends gcc git \
    && pip install --no-cache-dir -r requirements.txt \
    && pip uninstall spotAPI -y \
    && pip install git+https://github.com/TzurSoffer/SpotAPI \
    && apt-get remove -y git gcc \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Copy application code
COPY . .

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