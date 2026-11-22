# Texas Public Works OPCC Estimator -- the public demo as one container.
#
# The same file builds on linux/arm64 (Oracle Cloud Ampere A1) and
# linux/amd64 (Hugging Face Spaces, Render, Koyeb). Everything it copies is
# in this folder: src/ (the estimator and the bid-tab parser), ui/ (the web
# server, standard library only) and data/clean/ (the public data bundle:
# public lettings only, see data/MANIFEST.json).
#
#   docker build -t opcc-estimator .
#   docker run --rm -p 7860:7860 opcc-estimator          # http://localhost:7860
#   docker run ... -e DEMO_ACCESS_CODE=...               # optional access code
#
# The server runs in public-demo mode (BIDEST_PUBLIC=1, ui/public_mode.py):
# no server-side writes, no Claude reader, no local files, rate limits, a
# 25 MB upload cap. It listens on 0.0.0.0:$PORT (7860).
FROM python:3.12-slim

LABEL org.opencontainers.image.title="Texas Public Works OPCC Estimator" \
      org.opencontainers.image.source="https://github.com/grintCactus/texas-public-works-opcc-estimator"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# OCR of scanned PDFs (Tesseract, with the English and orientation data)
# and pdftotext (poppler) for text-layer PDFs.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-eng tesseract-ocr-osd poppler-utils \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --only-binary=:all: -r /app/requirements.txt

# src/ocr_scans.py looks for Tesseract under tools/tesseract (as on the
# development machine): point that path at the Debian package.
RUN set -eux; \
    tessdata="$(dirname "$(find /usr/share/tesseract-ocr -name eng.traineddata | head -n 1)")"; \
    test -f "$tessdata/osd.traineddata"; \
    mkdir -p /app/tools/tesseract/bin /app/tools/tesseract/share; \
    ln -s "$(command -v tesseract)" /app/tools/tesseract/bin/tesseract; \
    ln -s "$tessdata" /app/tools/tesseract/share/tessdata; \
    command -v pdftotext

COPY src/ /app/src/
COPY ui/ /app/ui/
COPY data/ /app/data/

# Byte-compile once at build time (the app directory is read-only at run
# time), then run as an unprivileged user (uid 1000, as Hugging Face expects).
RUN python -m compileall -q /app/src /app/ui \
 && useradd --create-home --uid 1000 app

USER app

ENV BIDEST_PUBLIC=1 \
    PORT=7860

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT', '7860'), timeout=4)" || exit 1

CMD ["python", "ui/run.py", "--public"]
