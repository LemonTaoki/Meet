FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    ca-certificates \
    curl \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir randomx certifi

COPY miner_1790628306101.py .

RUN mkdir -p /app/data
ENV PYTHONUNBUFFERED=1
ENV LOG_FILE=/app/data/miner.log
ENV STATE_FILE=/app/data/miner_state.json
ENV PAYOUT_HISTORY_FILE=/app/data/payout_history.json

CMD ["python", "miner_1790628306101.py"]
