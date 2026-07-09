#!/bin/sh
set -eu

mkdir -p /data /app/chatbot

if [ ! -f /data/sample_data.xlsx ]; then
  cp /app/sample_data.seed.xlsx /data/sample_data.xlsx
fi

if [ ! -f /data/auth_data.db ] && [ -f /app/auth_data.seed.db ]; then
  cp /app/auth_data.seed.db /data/auth_data.db
fi

cd /app/chatbot
python init_db.py

exec "$@"
