# DataExtractor design-time. Built from the repository root, because the image
# also carries the runtime: pattern learning runs every extraction through the
# real runtime in a child process (see src/dataextractor_designtime/learning).
#
#   docker build -f designtime/Dockerfile -t dataextractor-designtime .
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq-dev tesseract-ocr tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/*

# The runtime, exactly as it ships, for isolated extractions.
WORKDIR /app/runtime
COPY runtime/requirements.txt ./
RUN pip install -r requirements.txt
COPY runtime/extractor_tools/ extractor_tools/
COPY runtime/extractor_service/ extractor_service/
COPY runtime/configs/ configs/

WORKDIR /app
COPY designtime/pyproject.toml ./
COPY designtime/src ./src
COPY designtime/alembic ./alembic
COPY designtime/alembic.ini ./
RUN pip install -e ".[dev]"

# Learned config versions are written under RUNTIME_CONFIG_ROOT: mount the
# folder the runtime service serves there so it picks them up.
ENV PYTHONPATH=/app/src \
    RUNTIME_DIR=/app/runtime \
    RUNTIME_CONFIG_ROOT=/configs \
    LEARNING_INPUT_ROOT=/data \
    LEARNING_STATE_DIR=/learning-state
EXPOSE 8000

CMD ["uvicorn", "dataextractor_designtime.main:app", "--host", "0.0.0.0", "--port", "8000"]
