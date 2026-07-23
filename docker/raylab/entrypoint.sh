#!/bin/bash
set -e

ALEMBIC_INI=/app/models/db_schemes/raylab/alembic.ini

echo "Configuring Alembic database URL from environment..."
sed -i "s#^sqlalchemy.url = .*#sqlalchemy.url = postgresql://${POSTGRES_USERNAME}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_MAIN_DATABASE}#" "$ALEMBIC_INI"

echo "Running database migrations..."
cd /app/models/db_schemes/raylab
alembic upgrade head
cd /app

exec "$@"
