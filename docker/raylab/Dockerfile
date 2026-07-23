FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ .

# Alembic template for this container's own migration run at startup —
# alembic.ini itself carries real credentials, so it's built from the
# example here rather than copied from a tracked file (mirrors
# src/models/db_schemes/raylab/alembic.ini.example locally).
COPY docker/raylab/alembic.example.ini /app/models/db_schemes/raylab/alembic.ini

COPY docker/raylab/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
