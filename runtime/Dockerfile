# DataExtractor runtime: one self-contained image.
# Configs (data dictionaries, prompts, skills) are baked in under /app/configs;
# mount a folder over it to use your own. Input files are read from /data.
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY extractor_tools/ extractor_tools/
COPY extractor_service/ extractor_service/
COPY configs/ configs/

RUN useradd --system --uid 10001 --home /app extractor \
 && mkdir -p /data /audit \
 && chown extractor /data /audit

ENV CONFIG_ROOT=/app/configs \
    INPUT_ROOT=/data \
    AUDIT_DIR=/audit \
    MAX_FILE_MB=25
# Model calls go through the gateway: pass MODEL_GATEWAY_URL and MODEL_GATEWAY_TOKEN
# at run time (never bake credentials into the image).

USER extractor
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "extractor_service.api:app", "--host", "0.0.0.0", "--port", "8000"]
