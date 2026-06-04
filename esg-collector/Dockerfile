FROM python:3.11-slim

# lxml installs from a manylinux wheel on slim — no compiler/libxml2-dev needed.
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
