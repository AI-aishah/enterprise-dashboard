FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DASHBOARD_HOST=0.0.0.0 \
    DASHBOARD_PORT=8000 \
    DASHBOARD_DATA_DIR=/data \
    DASHBOARD_DB_PATH=/data/chatbot_data.db \
    DASHBOARD_AUTH_DB_PATH=/data/auth_data.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY chatbot ./chatbot
COPY dashboard_pages ./dashboard_pages
COPY sample_data.xlsx ./sample_data.xlsx
COPY docker-entrypoint.sh ./docker-entrypoint.sh

RUN chmod +x /app/docker-entrypoint.sh \
    && mkdir -p /data \
    && cp /app/sample_data.xlsx /app/sample_data.seed.xlsx \
    && if [ -f /app/chatbot/auth_data.db ]; then cp /app/chatbot/auth_data.db /app/auth_data.seed.db; fi

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import json, urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "server.py"]
