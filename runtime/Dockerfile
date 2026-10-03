# DataExtractor runtime: one self-contained image.
# Configs (data dictionaries, prompts, skills) are baked in under /app/configs;
# mount a folder over it to use your own. Input files are read from /data.
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tesseract: OCR for scanned PDF pages (and images, when a config turns it on)
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY extractor_tools/ extractor_tools/
COPY extractor_service/ extractor_service/
COPY configs/ configs/

RUN useradd --system --uid 10001 --home /app extractor \
 && mkdir -p /data /audit /state \
 && chown extractor /data /audit /state

# /state holds the job queue, checkpoints, the spool and the audit chain: mount a
# volume there, shared by every API and worker container of one deployment.
ENV CONFIG_ROOT=/app/configs \
    INPUT_ROOT=/data \
    AUDIT_DIR=/audit \
    STATE_DIR=/state \
    WORKERS=2 \
    PARSE_SANDBOX=process \
    MAX_FILE_MB=25
VOLUME ["/state"]
# Set STATE_KEY (a Fernet key) to encrypt spooled attachments at rest, and
# WEBHOOK_SECRET to sign webhook deliveries.
# Model calls go through the gateway: pass MODEL_GATEWAY_URL and MODEL_GATEWAY_TOKEN
# at run time (never bake credentials into the image).

USER extractor
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "extractor_service.api:app", "--host", "0.0.0.0", "--port", "8000"]
# Extra queue workers, same image: docker run ... dataextractor-runtime python -m extractor_service.worker
