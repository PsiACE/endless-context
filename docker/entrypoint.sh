#!/usr/bin/env bash
set -euo pipefail

start_seekdb() {
  if [ -n "${SEEKDB_START_CMD:-}" ]; then
    echo "Starting SeekDB with SEEKDB_START_CMD"
    bash -lc "${SEEKDB_START_CMD}" &
    return
  fi

  for cmd in /docker-entrypoint.sh /entrypoint.sh /usr/local/bin/docker-entrypoint.sh; do
    if [ -x "${cmd}" ]; then
      echo "Starting SeekDB with ${cmd}"
      "${cmd}" &
      return
    fi
  done

  if command -v seekdb >/dev/null 2>&1; then
    echo "Starting SeekDB with seekdb"
    seekdb &
    return
  fi

  if command -v observer >/dev/null 2>&1; then
    echo "Starting SeekDB with observer"
    observer &
    return
  fi

  if command -v obd >/dev/null 2>&1; then
    echo "Starting SeekDB with obd cluster start"
    obd cluster start &
    return
  fi

  echo "SeekDB start command not found. Set SEEKDB_START_CMD to override."
  exit 1
}

db_host="${OCEANBASE_HOST:-127.0.0.1}"
db_port="${OCEANBASE_PORT:-2881}"
db_user="${OCEANBASE_USER:-root}"
db_name="${OCEANBASE_DATABASE:-republic}"
db_pass="${OCEANBASE_PASSWORD:-${ROOT_PASSWORD:-}}"
start_local_seekdb="${START_LOCAL_SEEKDB:-auto}"

if [ "${start_local_seekdb}" = "auto" ]; then
  if [ "${db_host}" = "127.0.0.1" ] || [ "${db_host}" = "localhost" ]; then
    start_local_seekdb="true"
  else
    start_local_seekdb="false"
  fi
fi

if [ "${start_local_seekdb}" = "true" ]; then
  start_seekdb
  sleep "${SEEKDB_START_DELAY:-2}"
else
  echo "Using external SeekDB at ${db_host}:${db_port}, skip local startup"
fi

# Wait for SeekDB to be ready and create database
echo "Waiting for SeekDB to be ready..."
max_attempts=30
attempt=0
while [ $attempt -lt $max_attempts ]; do
  if mysql -h"${db_host}" -P"${db_port}" -u"${db_user}" ${db_pass:+-p${db_pass}} -e "SELECT 1" >/dev/null 2>&1; then
    echo "SeekDB is ready!"
    break
  fi
  attempt=$((attempt + 1))
  echo "Waiting for SeekDB at ${db_host}:${db_port}... ($attempt/$max_attempts)"
  sleep 2
done

if [ $attempt -eq $max_attempts ]; then
  echo "SeekDB failed to start in time"
  exit 1
fi

# Create database if it doesn't exist
echo "Creating database ${db_name} if not exists..."
mysql -h"${db_host}" -P"${db_port}" -u"${db_user}" ${db_pass:+-p${db_pass}} -e "CREATE DATABASE IF NOT EXISTS \`${db_name}\`;" || {
  echo "Failed to create database"
  exit 1
}

echo "Database setup complete!"

# Place Bub built-in skill scripts into workspace so tools/skills can spawn them
uv run --no-dev python /usr/local/bin/setup-bub-workspace.py || true

# Replace skill-installer's install script with project copy (SSL + git fallback)
if [ -f /app/scripts/install-skill-from-github.py ] && [ -d /app/.agent/skills/skill-installer/scripts ]; then
  cp /app/scripts/install-skill-from-github.py /app/.agent/skills/skill-installer/scripts/install-skill-from-github.py
fi

# Project-local skills when agent runs install-skill-from-github.py
export BUB_SKILLS_HOME="${BUB_SKILLS_HOME:-/app/.agent/skills}"

exec uv run --no-dev python app.py
