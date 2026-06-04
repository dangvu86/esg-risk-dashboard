FROM python:3.11-slim

# lxml needs libxml2/libxslt at runtime; build from wheels so no compiler needed.
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Writable SQLite + outputs live in /tmp on Cloud Run (only writable path).
ENV ESG_DATA_DIR=/tmp/esg-data
ENV PYTHONUNBUFFERED=1

# Default to a daily run; the backfill job overrides --args.
ENTRYPOINT ["python", "-m", "runtime.job"]
CMD ["--mode", "daily"]
