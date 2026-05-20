# VCE Hub — practice-paper generator
#
# Single-image deploy: bakes the SQLite DB and asset PNGs into the image so the
# container is self-contained. The Microsoft Playwright Python base image has
# Chromium + Playwright pre-installed (saves ~300MB of `playwright install` work).
#
# Build size budget: corpus assets ~50MB + base image ~600MB ≈ 700MB total.
# If asset corpus grows past ~500MB, switch to a Fly Volume mounted at /srv/assets.

FROM mcr.microsoft.com/playwright/python:v1.59.0-jammy

WORKDIR /srv

# Install Python deps first (better layer caching).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App code, runtime data, and templates. .dockerignore excludes the offline-only
# bits (mathematical_methods/, pipeline/, raw exam PDFs, model caches, etc).
COPY app/ ./app/
COPY pipeline/db.py ./pipeline/db.py
COPY pipeline/__init__.py ./pipeline/__init__.py
COPY data/methods.db ./data/methods.db
COPY assets/ ./assets/
COPY migrations/ ./migrations/

# Public deploy: ADMIN unset → all /admin/* routes return 404.
ENV PYTHONUNBUFFERED=1 \
    PORT=8080

EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
