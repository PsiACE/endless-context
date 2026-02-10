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

start_seekdb
sleep "${SEEKDB_START_DELAY:-2}"

# Wait for SeekDB to be ready and create database
echo "Waiting for SeekDB to be ready..."
max_attempts=30
attempt=0
while [ $attempt -lt $max_attempts ]; do
  if mysql -h127.0.0.1 -P2881 -uroot ${ROOT_PASSWORD:+-p${ROOT_PASSWORD}} -e "SELECT 1" >/dev/null 2>&1; then
    echo "SeekDB is ready!"
    break
  fi
  attempt=$((attempt + 1))
  echo "Waiting for SeekDB... ($attempt/$max_attempts)"
  sleep 2
done

if [ $attempt -eq $max_attempts ]; then
  echo "SeekDB failed to start in time"
  exit 1
fi

# Create powermem database if it doesn't exist
echo "Creating powermem database if not exists..."
mysql -h127.0.0.1 -P2881 -uroot ${ROOT_PASSWORD:+-p${ROOT_PASSWORD}} -e "CREATE DATABASE IF NOT EXISTS powermem;" || {
  echo "Failed to create database"
  exit 1
}

echo "Database setup complete!"

exec uv run python app.py
